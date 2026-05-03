import os
import time
import uuid
import json
import asyncio
import logging
import re
from pyrogram import Client, filters, StopPropagation
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums import ParseMode
from config import Config
from helper_func.progress_bar import progress_bar

logger = logging.getLogger("extractor")

# --- MEMORY STATES ---
# Tracks who is waiting to send a file for extraction
EXTRACT_WAITING = {}
# Stores the probed video data so the buttons know what to extract
PROBED_TASKS = {}

# Map FFmpeg codec names to standard file extensions
CODEC_TO_EXT = {
    'subrip': 'srt',
    'ass': 'ass',
    'webvtt': 'vtt',
    'mov_text': 'srt',
    'hdmv_pgs_subtitle': 'sup',  # Note: Image-based, but can be extracted
    'dvd_subtitle': 'sub'        # Note: Image-based
}

async def _check_user(filt, c, m):
    if not m.from_user: return False
    return str(m.from_user.id) in Config.ALLOWED_USERS
check_user = filters.create(_check_user)

async def _is_waiting_extract(filt, c, m):
    return m.from_user and m.from_user.id in EXTRACT_WAITING
is_waiting_extract = filters.create(_is_waiting_extract)

# ==========================================
# 1. THE COMMAND HANDLER
# ==========================================
@Client.on_message(filters.command("extract") & check_user & filters.private)
async def extract_cmd(client, message):
    chat_id = message.from_user.id
    parts = message.text.split(maxsplit=1)

    # Usage 1: /extract [URL]
    if len(parts) == 2 and parts[1].strip().startswith("http"):
        url = parts[1].strip()
        await process_probe(client, message.chat.id, url, is_url=True)
    
    # Usage 2: /extract (Wait for file)
    else:
        EXTRACT_WAITING[chat_id] = True
        await message.reply(
            "📤 **Extraction Mode Activated!**\n\n"
            "Send me the **Video File** or a **Direct Download Link** you want to extract subtitles from.\n"
            "*(Send /cancel to exit extraction mode)*",
            parse_mode=ParseMode.MARKDOWN
        )

# ==========================================
# 2. THE INTERCEPTOR (Prevents queue conflict)
# ==========================================
# group=-1 runs BEFORE your save_file.py!
@Client.on_message(is_waiting_extract & (filters.video | filters.document | filters.text) & filters.private, group=-1)
async def handle_extract_input(client, message):
    chat_id = message.from_user.id
    
    if message.text and message.text.startswith("/cancel"):
        EXTRACT_WAITING.pop(chat_id, None)
        await message.reply("🛑 Extraction mode cancelled.")
        raise StopPropagation

    if message.text and message.text.startswith("/"):
        return # Let other commands pass through

    EXTRACT_WAITING.pop(chat_id, None) # Remove from waiting state
    
    if message.text and message.text.startswith("http"):
        # It's a URL
        url = message.text.strip()
        await process_probe(client, chat_id, url, is_url=True)
        raise StopPropagation

    elif message.video or message.document:
        # It's a Telegram File, we must download it first
        start_time = time.time()
        status_msg = await message.reply('📥 Downloading Video for Extraction...')
        
        try:
            download_path = await client.download_media(
                message=message,
                file_name=os.path.join(Config.DOWNLOAD_DIR, f"ext_{uuid.uuid4().hex[:6]}.mkv"),
                progress=progress_bar,
                progress_args=('📥 Downloading Video for Extraction...', status_msg, start_time)
            )
        except Exception as e:
            await status_msg.edit(f"❌ Download failed: {e}")
            raise StopPropagation

        original_name = getattr(message.document, 'file_name', getattr(message.video, 'file_name', "video.mkv"))
        
        await status_msg.delete()
        await process_probe(client, chat_id, download_path, is_url=False, original_name=original_name)
        raise StopPropagation

# ==========================================
# 3. PROBING LOGIC (The Magic Scanner)
# ==========================================
async def process_probe(client, chat_id, path_or_url, is_url, original_name="stream_video"):
    status = await client.send_message(chat_id, "🔍 Scanning file for subtitle streams...")
    
    # Increase buffer massively to find hidden streams in remote files
    cmd = ['ffprobe', '-v', 'error', '-analyzeduration', '100M', '-probesize', '100M']
    
    if is_url:
        # Spoof a real Chrome Browser to bypass API/CDN blockers
        cmd.extend(['-user_agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'])
        
    cmd.extend([
        '-select_streams', 's',
        '-show_entries', 'stream=index,codec_name:stream_tags=language,title',
        '-of', 'json', path_or_url
    ])
    
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await proc.communicate()
    
    if proc.returncode != 0:
        logger.error(f"Probe failed: {stderr.decode()}")
        return await status.edit("❌ Failed to scan the file. It might not be a valid video or direct link.")

    try:
        data = json.loads(stdout)
        streams = data.get('streams', [])
    except Exception as e:
        return await status.edit("❌ Error parsing video data.")

    if not streams:
        return await status.edit("⚠️ **No subtitle streams found in this video!**", parse_mode=ParseMode.MARKDOWN)

    # Generate a unique Task ID and store the data
    task_id = uuid.uuid4().hex[:8]
    PROBED_TASKS[task_id] = {
        "path": path_or_url,
        "is_url": is_url,
        "name": os.path.splitext(original_name)[0] if not is_url else "Extracted_Sub",
        "streams": streams
    }

    # Build the Interactive Buttons
    buttons = []
    text = f"✅ **Found {len(streams)} Subtitle Stream(s)!**\n\nSelect a stream to extract:\n"
    
    for idx, s in enumerate(streams):
        s_idx = s.get('index')
        codec = s.get('codec_name', 'unknown')
        tags = s.get('tags', {})
        lang = tags.get('language', 'und').upper()
        title = tags.get('title', '')
        
        btn_label = f"[{lang}] {title} ({codec})" if title else f"Track {idx+1} - [{lang}] ({codec})"
        
        # FIX: Using colons (:) instead of underscores to prevent splitting bugs on codecs like "mov_text"
        callback_data = f"extsub:{task_id}:{s_idx}:{codec}:{lang}"
        
        buttons.append([InlineKeyboardButton(btn_label, callback_data=callback_data)])
    
    # Add a Close/Cleanup button (Using colons here too)
    buttons.append([InlineKeyboardButton("❌ Close & Cleanup", callback_data=f"extclose:{task_id}")])

    await status.edit(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.MARKDOWN)

# ==========================================
# 4. EXTRACTION BUTTON HANDLERS
# ==========================================
@Client.on_callback_query(filters.regex(r'^extsub:'))
async def extract_callback(client, callback_query):
    # FIX: Splitting by colon now
    _, task_id, stream_idx, codec, lang = callback_query.data.split(":")
    
    task = PROBED_TASKS.get(task_id)
    if not task:
        return await callback_query.answer("⚠️ Session expired. Please send the link/file again.", show_alert=True)

    await callback_query.answer("Extracting... Please wait.", show_alert=False)
    
    status = await callback_query.message.reply("⚙️ Extracting subtitle stream...")
    
    ext = CODEC_TO_EXT.get(codec, 'srt') # Default to srt if unknown
    output_name = f"{task['name']}_{lang}_{stream_idx}.{ext}"
    output_path = os.path.join(Config.DOWNLOAD_DIR, output_name)

    cmd = ['ffmpeg', '-hide_banner', '-v', 'error']
    
    if task['is_url']:
        cmd.extend(['-user_agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'])
        
    cmd.extend([
        '-i', task['path'],
        '-map', f'0:{stream_idx}',
        '-c:s', 'copy',
        '-y', output_path
    ])

    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, stderr = await proc.communicate()

    if proc.returncode == 0 and os.path.exists(output_path):
        await status.edit("📤 Uploading extracted subtitle...")
        await client.send_document(
            chat_id=callback_query.from_user.id,
            document=output_path,
            caption=f"✅ Extracted Track `{stream_idx}`\n**Language:** {lang}\n**Format:** {ext.upper()}",
            parse_mode=ParseMode.MARKDOWN
        )
        os.remove(output_path)
        await status.delete()
    else:
        logger.error(f"Extraction failed: {stderr.decode()}")
        await status.edit("❌ Failed to extract subtitle. The codec might not support raw extraction.")

@Client.on_callback_query(filters.regex(r'^extclose:'))
async def close_extract(client, callback_query):
    # FIX 1: Must explicitly answer the callback query so the button doesn't get stuck!
    await callback_query.answer("Cleaning up...", show_alert=False)
    
    _, task_id = callback_query.data.split(":")
    
    task = PROBED_TASKS.pop(task_id, None)
    if task and not task['is_url']:
        # If it was a downloaded file, delete it from the server to save space
        try: os.remove(task['path'])
        except: pass

    # FIX 2: Use edit_message_text instead of message.edit for cleaner callback handling
    try:
        await callback_query.edit_message_text("🧹 **Session Closed and Cleaned Up.**", parse_mode=ParseMode.MARKDOWN)
    except:
        pass
