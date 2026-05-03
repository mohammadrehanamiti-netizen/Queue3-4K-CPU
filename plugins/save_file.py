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
try:
    from curl_cffi import requests as curl_requests
except Exception:
    curl_requests = None

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
DM_AUDIO_RE = re.compile(r'GROUP-ID="(?P<group>[^"]+)".*?(?:DEFAULT=(?P<default>YES|NO)).*?URI="(?P<uri>[^"]+)"')
DM_STREAM_RE = re.compile(r'BANDWIDTH=(?P<bandwidth>\d+).*?(?:RESOLUTION=(?P<resolution>\d+x\d+))?.*?(?:AUDIO="(?P<audio>[^"]+)")?')


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


def _pick_best_dm_stream(master_manifest: str):
    audio_groups = {}
    best_variant = None
    pending_stream = None

    for raw_line in master_manifest.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("#EXT-X-MEDIA:TYPE=AUDIO"):
            match = DM_AUDIO_RE.search(line)
            if match:
                group = match.group("group")
                audio_groups.setdefault(group, []).append(
                    {
                        "uri": match.group("uri"),
                        "default": match.group("default") == "YES",
                    }
                )
            continue

        if line.startswith("#EXT-X-STREAM-INF:"):
            match = DM_STREAM_RE.search(line)
            if match:
                pending_stream = {
                    "bandwidth": int(match.group("bandwidth")),
                    "resolution": match.group("resolution"),
                    "audio_group": match.group("audio"),
                }
            else:
                pending_stream = None
            continue

        if pending_stream and line.startswith("http"):
            candidate = pending_stream | {"video_url": line}
            if best_variant is None or candidate["bandwidth"] > best_variant["bandwidth"]:
                best_variant = candidate
            pending_stream = None

    if best_variant is None:
        raise RuntimeError("Could not find any playable Dailymotion streams in the master playlist.")

    audio_url = None
    audio_group = best_variant.get("audio_group")
    if audio_group and audio_group in audio_groups:
        preferred = next((item for item in audio_groups[audio_group] if item["default"]), audio_groups[audio_group][0])
        audio_url = preferred["uri"]
    elif audio_groups:
        first_group = next(iter(audio_groups.values()))
        if first_group:
            preferred = next((item for item in first_group if item["default"]), first_group[0])
            audio_url = preferred["uri"]

    return best_variant["video_url"], audio_url


def _build_unique_output_path(dest_dir: str, title: str, video_id: str):
    base = _safe_filename(title or f"dailymotion_{video_id}")[:180].strip(" .") or f"dailymotion_{video_id}"
    filename = f"{base} [{video_id}].mp4"
    full_path = os.path.join(dest_dir, filename)
    if not os.path.exists(full_path):
        return full_path
    return os.path.join(dest_dir, f"{base} [{video_id}]_{uuid.uuid4().hex[:6]}.mp4")


async def _download_dailymotion_via_browser_manifest(url: str, dest_dir: str, status_msg, job_id: str):
    if curl_requests is None:
        raise RuntimeError("curl_cffi is not installed, so the Dailymotion browser fallback is unavailable.")

    os.makedirs(dest_dir, exist_ok=True)
    headers = {
        "Referer": "https://www.dailymotion.com/",
        "Origin": "https://www.dailymotion.com",
    }
    _, normalized_url = build_ytdlp_base_command(url)
    video_id = normalized_url.rstrip("/").split("/")[-1]
    metadata_url = f"https://www.dailymotion.com/player/metadata/video/{video_id}?embedder=https://www.dailymotion.com/"

    await safe_edit_message(
        status_msg,
        (
            "<b>Dailymotion fallback enabled...</b>\n\n"
            "The normal extractor was blocked by Dailymotion, so the bot is switching to the player-manifest path."
        ),
        parse_mode=ParseMode.HTML,
        force=True,
    )

    metadata_resp = curl_requests.get(metadata_url, timeout=30, headers=headers, impersonate="chrome")
    metadata_resp.raise_for_status()
    metadata = metadata_resp.json()

    qualities = metadata.get("qualities") or {}
    auto_entries = qualities.get("auto") or []
    master_url = next((entry.get("url") for entry in auto_entries if entry.get("url")), None)
    if not master_url:
        raise RuntimeError("Dailymotion metadata did not include a master playlist URL.")

    master_resp = curl_requests.get(master_url, timeout=30, headers=headers, impersonate="chrome")
    master_resp.raise_for_status()
    video_url, audio_url = _pick_best_dm_stream(master_resp.text)

    output_path = _build_unique_output_path(dest_dir, metadata.get("title"), metadata.get("id") or video_id)

    await safe_edit_message(
        status_msg,
        (
            "<b>Downloading Dailymotion streams...</b>\n\n"
            f"Job ID: <code>{job_id}</code>\n"
            f"Video ID: <code>{metadata.get('id') or video_id}</code>\n"
            "Merging the best video stream with its audio stream."
        ),
        parse_mode=ParseMode.HTML,
        force=True,
    )

    ffmpeg_cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        video_url,
    ]
    if audio_url:
        ffmpeg_cmd += ["-i", audio_url, "-map", "0:v:0", "-map", "1:a:0?"]
    else:
        ffmpeg_cmd += ["-map", "0:v:0", "-map", "0:a:0?"]

    ffmpeg_cmd += ["-c", "copy", "-movflags", "+faststart", "-y", output_path]

    proc = await asyncio.create_subprocess_exec(
        *ffmpeg_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0 or not os.path.exists(output_path):
        raise RuntimeError(
            "ffmpeg failed while downloading the Dailymotion audio/video streams.\n"
            f"{stderr.decode(errors='ignore')[-1200:]}"
        )

    return os.path.basename(output_path)


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
            try:
                saved_name = await _download_dailymotion_with_ytdlp(
                    url=url,
                    dest_dir=Config.DOWNLOAD_DIR,
                    status_msg=sent,
                    job_id=job_id,
                )
            except Exception as dm_error:
                logger.warning("yt-dlp Dailymotion download failed, trying browser-manifest fallback: %s", dm_error)
                saved_name = await _download_dailymotion_via_browser_manifest(
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
