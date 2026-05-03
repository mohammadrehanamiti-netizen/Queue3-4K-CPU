import asyncio
import logging
import os
import re
import time
import uuid
from urllib.parse import unquote, urlparse

import aiohttp
from pyrogram import Client, filters
from pyrogram.enums import ParseMode

from chat import Chat
from config import Config
from helper_func.dbhelper import Database as Db
from helper_func.message_utils import safe_edit_message
from helper_func.progress_bar import progress_bar
from helper_func.source_utils import build_ytdlp_base_command, is_dailymotion_url

logger = logging.getLogger(__name__)
logging.getLogger("pyrogram").setLevel(logging.WARNING)

db = Db()


async def _check_user(filt, c, m):
    if not m.from_user:
        return False
    chat_id = str(m.from_user.id)
    return chat_id in Config.ALLOWED_USERS


check_user = filters.create(_check_user)


FILENAME_RE = re.compile(r'filename\*?=(?:UTF-8\'\')?("?)([^";]+)\1', re.IGNORECASE)
YT_DLP_PROGRESS_RE = re.compile(
    r"\[download\]\s+(?P<pct>\d+(?:\.\d+)?)%.*?of\s+(?P<size>.+?)(?:\s+at\s+(?P<speed>.+?))?(?:\s+ETA\s+(?P<eta>\S+))?$",
    re.IGNORECASE,
)


def _safe_filename(name: str) -> str:
    name = os.path.basename(name)
    name = name.replace("\r", "").replace("\n", "").strip()
    return re.sub(r'[\\/:*?"<>|]+', "_", name)


def _pick_name_from_url(url: str) -> str:
    path = urlparse(url).path
    tail = os.path.basename(path) or "download.bin"
    return _safe_filename(unquote(tail))


def _maybe_add_ext(name: str, content_type: str) -> str:
    if (not os.path.splitext(name)[1]) and content_type and content_type.startswith("video/"):
        return name + ".mp4"
    return name


async def _download_http_with_progress(url: str, dest_dir: str, status_msg, start_time: float, job_id: str | None):
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=60, sock_read=600)
    headers = {"User-Agent": "Mozilla/5.0 (QueueBot/1.0)"}

    async with aiohttp.ClientSession(timeout=timeout, headers=headers, raise_for_status=True) as session:
        async with session.get(url, allow_redirects=True) as resp:
            total = int(resp.headers.get("Content-Length", "0") or 0)

            filename = None
            content_disposition = resp.headers.get("Content-Disposition")
            if content_disposition:
                match = FILENAME_RE.search(content_disposition)
                if match:
                    filename = _safe_filename(match.group(2))

            if not filename:
                filename = _pick_name_from_url(str(resp.url))
            filename = _maybe_add_ext(filename, resp.headers.get("Content-Type", ""))

            base, ext = os.path.splitext(filename)
            unique_name = f"{base}_{uuid.uuid4().hex[:6]}{ext}"
            full_path = os.path.join(dest_dir, unique_name)

            downloaded = 0
            chunk_size = 1024 * 1024
            with open(full_path, "wb") as handle:
                async for chunk in resp.content.iter_chunked(chunk_size):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    downloaded += len(chunk)

                    await progress_bar(
                        downloaded,
                        total,
                        "Downloading from Link...",
                        status_msg,
                        start_time,
                        job_id=job_id,
                    )
    return unique_name


async def _stream_lines(stream):
    while True:
        line = await stream.readline()
        if not line:
            break
        yield line.decode(errors="ignore").strip()


async def _read_ytdlp_stdout(stream, seen_lines):
    async for line in _stream_lines(stream):
        if line:
            seen_lines.append(line)


async def _read_ytdlp_progress(stream, status_msg, job_id):
    stderr_lines = []
    async for line in _stream_lines(stream):
        if not line:
            continue
        stderr_lines.append(line)

        match = YT_DLP_PROGRESS_RE.search(line)
        if not match:
            continue

        speed = match.group("speed") or "Unknown"
        eta = match.group("eta") or "Unknown"
        card = (
            "<b>Downloading from Dailymotion...</b>\n\n"
            f"Job ID: <code>{job_id}</code>\n"
            f"Progress: <b>{match.group('pct')}%</b>\n"
            f"Size: {match.group('size')}\n"
            f"Speed: {speed}\n"
            f"ETA: {eta}"
        )
        await safe_edit_message(status_msg, card, parse_mode=ParseMode.HTML, min_interval=8.0)

    return stderr_lines


async def _download_dailymotion_with_ytdlp(url: str, dest_dir: str, status_msg, job_id: str):
    os.makedirs(dest_dir, exist_ok=True)
    output_template = os.path.join(dest_dir, "%(title).180B [%(id)s].%(ext)s")
    cmd, normalized_url = build_ytdlp_base_command(url)
    cmd += [
        "--newline",
        "--progress",
        "--no-part",
        "--restrict-filenames",
        "--merge-output-format",
        "mp4",
        "-f",
        "bv*+ba/b",
        "--print",
        "after_move:filepath",
        "-o",
        output_template,
        normalized_url,
    ]

    await safe_edit_message(
        status_msg,
        (
            "<b>Preparing Dailymotion download...</b>\n\n"
            "This source uses separate video and audio streams, so both will be fetched and merged."
        ),
        parse_mode=ParseMode.HTML,
        force=True,
    )

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout_lines = []
    stdout_task = asyncio.create_task(_read_ytdlp_stdout(proc.stdout, stdout_lines))
    stderr_task = asyncio.create_task(_read_ytdlp_progress(proc.stderr, status_msg, job_id))
    wait_task = asyncio.create_task(proc.wait())

    await asyncio.wait([stdout_task, stderr_task, wait_task])

    stderr_lines = stderr_task.result() or []
    final_path = None
    for line in reversed(stdout_lines):
        if os.path.exists(line):
            final_path = line
            break

    if proc.returncode != 0 or not final_path:
        tail = "\n".join(stderr_lines[-10:])
        raise RuntimeError(f"yt-dlp failed to download the Dailymotion link.\n{tail}".strip())

    return os.path.basename(final_path)


@Client.on_message(filters.document & check_user & filters.private)
async def save_doc(client, message):
    chat_id = message.from_user.id
    start_time = time.time()
    downloading = await client.send_message(chat_id, "Preparing download...")

    download_location = await client.download_media(
        message=message,
        file_name=Config.DOWNLOAD_DIR + "/",
        progress=progress_bar,
        progress_args=("Downloading Document...", downloading, start_time),
    )

    if download_location is None:
        return await safe_edit_message(downloading, "Download failed!", force=True)

    tg_filename = os.path.basename(download_location)
    og_filename = getattr(message.document, "file_name", None) if message.document else None

    save_filename = og_filename if og_filename else tg_filename
    ext = save_filename.split(".").pop().lower()
    filename = str(round(start_time)) + "." + ext

    if ext in ["srt", "ass"]:
        os.rename(Config.DOWNLOAD_DIR + "/" + tg_filename, Config.DOWNLOAD_DIR + "/" + filename)
        db.put_sub(chat_id, filename)
        if db.check_video(chat_id):
            text = "Subtitle file downloaded successfully.\nChoose[ /softmux , /hardmux , /nosub ]"
        else:
            text = "Subtitle file downloaded successfully.\nNow send Video File!"
        await safe_edit_message(downloading, text, force=True)

    elif ext in ["mp4", "mkv"]:
        os.rename(Config.DOWNLOAD_DIR + "/" + tg_filename, Config.DOWNLOAD_DIR + "/" + filename)
        db.put_video(chat_id, filename, save_filename)
        text = "Video file downloaded successfully.\nChoose[ /softmux , /hardmux , /nosub ]"
        await safe_edit_message(downloading, text, force=True)

    else:
        text = Chat.UNSUPPORTED_FORMAT.format(ext, tg_filename)
        await safe_edit_message(downloading, text, parse_mode=ParseMode.HTML, force=True)
        os.remove(Config.DOWNLOAD_DIR + "/" + tg_filename)


@Client.on_message(filters.video & check_user & filters.private)
async def save_video(client, message):
    chat_id = message.from_user.id
    start_time = time.time()
    downloading = await client.send_message(chat_id, "Preparing download...")

    download_location = await client.download_media(
        message=message,
        file_name=Config.DOWNLOAD_DIR + "/",
        progress=progress_bar,
        progress_args=("Downloading Video...", downloading, start_time),
    )

    if download_location is None:
        return await safe_edit_message(downloading, "Download failed!", force=True)

    tg_filename = os.path.basename(download_location)
    og_filename = getattr(message.video, "file_name", None) if message.video else None

    save_filename = og_filename if og_filename else tg_filename
    ext = save_filename.split(".").pop().lower()
    filename = str(round(start_time)) + "." + ext
    os.rename(Config.DOWNLOAD_DIR + "/" + tg_filename, Config.DOWNLOAD_DIR + "/" + filename)

    db.put_video(chat_id, filename, save_filename)
    text = "Video file downloaded successfully.\nChoose[ /softmux , /hardmux , /nosub ]"
    await safe_edit_message(downloading, text, force=True)


@Client.on_message(filters.text & filters.regex("^http") & check_user & filters.private)
async def save_url(client, message):
    chat_id = message.from_user.id
    url = message.text.strip()
    sent = await client.send_message(chat_id, "Fetching link...")
    t0 = time.time()

    if url.lower().endswith(".m3u8") or "m3u8" in url.lower():
        db.put_video(chat_id, url, _pick_name_from_url(url))
        if db.check_sub(chat_id):
            text = "HLS link captured.\nChoose[ /softmux , /hardmux , /nosub ]"
        else:
            text = "HLS link captured. Send a subtitle file, or use /nosub to encode without subtitles."
        await safe_edit_message(sent, text, force=True)
        return

    try:
        os.makedirs(Config.DOWNLOAD_DIR, exist_ok=True)
        job_id = uuid.uuid4().hex[:8]

        if is_dailymotion_url(url):
            saved_name = await _download_dailymotion_with_ytdlp(
                url=url,
                dest_dir=Config.DOWNLOAD_DIR,
                status_msg=sent,
                job_id=job_id,
            )
        else:
            saved_name = await _download_http_with_progress(
                url=url,
                dest_dir=Config.DOWNLOAD_DIR,
                status_msg=sent,
                start_time=t0,
                job_id=job_id,
            )

        db.put_video(chat_id, saved_name, saved_name)
        text = "Video file downloaded successfully.\nChoose[ /softmux , /hardmux , /nosub ]"
        await safe_edit_message(sent, text, force=True)

    except Exception as exc:
        try:
            await safe_edit_message(
                sent,
                f"Failed to download from link.\n<code>{str(exc)}</code>",
                parse_mode=ParseMode.HTML,
                force=True,
            )
        except Exception:
            pass
