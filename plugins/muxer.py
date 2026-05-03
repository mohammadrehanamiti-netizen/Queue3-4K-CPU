from chat import Chat  
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums import ParseMode
from helper_func.queue import Job, job_queue
from helper_func.mux import softmux_vid, hardmux_vid, nosub_encode, running_jobs, generate_thumbnail, _humanbytes, _fmt_time, split_video
from helper_func.progress_bar import progress_bar
from helper_func.dbhelper import Database as Db
from helper_func.settings_manager import SettingsManager
from config import Config
import uuid, time, os, asyncio, sys, sqlite3
import logging

logger = logging.getLogger("muxer")
db = Db()
_PENDING_RENAME = {} 

async def _check_user(filt, client, message):
    if not message.from_user: return False
    return str(message.from_user.id) in Config.ALLOWED_USERS
check_user = filters.create(_check_user)

async def _is_pending_rename(filt, c, m):
    if not m.text or m.text.startswith("/"): return False
    return m.from_user.id in _PENDING_RENAME
is_pending_rename_filter = filters.create(_is_pending_rename)

async def _ask_for_name(client, chat_id, mode, vid, sub, default_name):
    status = await client.send_message(
        chat_id,
        text=Chat.RENAME_PROMPT.format(default_name),
        parse_mode=ParseMode.HTML
    )
    _PENDING_RENAME[chat_id] = dict(mode=mode, vid=vid, sub=sub, default_name=default_name, status_msg=status)

# --------------------- COMMANDS ---------------------

async def clean_bot_prompts(client, chat_id):
    """Hunts down and deletes the bot's previous menu prompts"""
    try:
        # Scan the last 5 messages in the chat
        async for msg in client.get_chat_history(chat_id, limit=5):
            if msg.from_user and msg.from_user.is_self:
                # Check both text messages and media captions
                text = msg.text or msg.caption or ""
                if "Choose[ /softmux" in text or "Video file downloaded" in text:
                    await msg.delete()
    except:
        pass

@Client.on_message(filters.command('softmux') & check_user & filters.private)
async def enqueue_soft(client, message):
    try: await message.delete() # Delete user command
    except: pass
    
    chat_id = message.from_user.id
    await clean_bot_prompts(client, chat_id) # HUNT AND DELETE BOT PROMPT
    
    vid, sub = db.get_vid_filename(chat_id), db.get_sub_filename(chat_id)
    if not vid or not sub:
        return await client.send_message(chat_id, "First send a Video & Subtitle File!", parse_mode=ParseMode.HTML)
    await _ask_for_name(client, chat_id, 'soft', vid, sub, db.get_filename(chat_id))

@Client.on_message(filters.command('hardmux') & check_user & filters.private)
async def enqueue_hard(client, message):
    try: await message.delete() # Delete user command
    except: pass
    
    chat_id = message.from_user.id
    await clean_bot_prompts(client, chat_id) # HUNT AND DELETE BOT PROMPT
    
    vid, sub = db.get_vid_filename(chat_id), db.get_sub_filename(chat_id)
    if not vid or not sub:
        return await client.send_message(chat_id, "First send a Video & Subtitle File!", parse_mode=ParseMode.HTML)
    await _ask_for_name(client, chat_id, 'hard', vid, sub, db.get_filename(chat_id))
    
@Client.on_message(filters.command('nosub') & check_user & filters.private)
async def enqueue_nosub(client, message):
    try: await message.delete() # Delete user command
    except: pass
    
    chat_id = message.from_user.id
    await clean_bot_prompts(client, chat_id) # HUNT AND DELETE BOT PROMPT
    
    vid = db.get_vid_filename(chat_id)
    if not vid:
        return await client.send_message(chat_id, 'First send a Video File', parse_mode=ParseMode.HTML)
    await _ask_for_name(client, chat_id, 'nosub', vid, None, db.get_filename(chat_id))


@Client.on_message(filters.text & check_user & filters.private & is_pending_rename_filter)
async def handle_rename_reply(client, message):
    chat_id = message.from_user.id
    pending = _PENDING_RENAME.pop(chat_id, None)
    if not pending: return

    user_text = message.text.strip()
    final_name = pending["default_name"] if user_text.lower() == "default" else user_text

    try: await pending["status_msg"].delete() # Delete bot's prompt
    except: pass
    try: await message.delete() # Delete user's reply message
    except: pass

    job_id = uuid.uuid4().hex[:8]
    status = await client.send_message(
        chat_id,
        f"🧾 Job <code>{job_id}</code> (<code>{final_name}</code>) enqueued at position {job_queue.qsize() + 1}",
        parse_mode=ParseMode.HTML
    )

    user_settings = SettingsManager.get(chat_id).copy()
    await job_queue.put(Job(
        job_id=job_id, mode=pending["mode"], chat_id=chat_id,
        vid=pending["vid"], sub=pending["sub"], final_name=final_name,
        status_msg=status, settings=user_settings
    ))
    db.erase(chat_id)


@Client.on_message(filters.command('m3u8') & check_user & filters.private)
async def enqueue_m3u8(client, message):
    parts = message.text.split(maxsplit=2)
    try: await message.delete() # CLEANUP
    except: pass
    
    if len(parts) < 2: return await message.reply_text("Usage:\n/m3u8 <m3u8_url> [output_name.mp4]")
    url = parts[1].strip()
    if not (url.startswith("http://") or url.startswith("https://")) or ".m3u8" not in url:
        return await message.reply_text("Please provide a valid .m3u8 URL.")

    final_name = parts[2].strip() if len(parts) == 3 else f"{uuid.uuid4().hex[:6]}_enc.mp4"
    chat_id = message.from_user.id
    db.erase(chat_id)
    db.set_vid_filename(chat_id, url)
    db.set_filename(chat_id, final_name)

    job_id  = uuid.uuid4().hex[:8]
    status  = await client.send_message(chat_id, f"🧾 Job <code>{job_id}</code> enqueued...", parse_mode=ParseMode.HTML)
    
    user_settings = SettingsManager.get(chat_id).copy()
    await job_queue.put(Job(job_id, 'nosub', chat_id, url, None, final_name, status, user_settings))

# ================= STATUS COMMAND UI =================

def build_status_text():
    text = "<b>💻 Encoding Server Status</b>\n\n"
    if not running_jobs:
        text += "<b>▶️ Processing:</b> <i>Idle (No active jobs)</i>\n\n"
    else:
        text += "<b>▶️ Currently Processing:</b>\n"
        for jid, data in running_jobs.items():
            prog_card = data.get('progress', f"Initializing job {jid}...")
            text += f"━━━━━━━━━━━━━━━━━━━━\n{prog_card}\n"
    
    text += "━━━━━━━━━━━━━━━━━━━━\n"
    q_list = list(job_queue._queue)
    if not q_list:
        text += "<b>⏳ Queued Jobs:</b> 0\n"
    else:
        text += f"<b>⏳ Queued Jobs ({len(q_list)}):</b>\n"
        for i, job in enumerate(q_list, 1):
            cfg_str = "Default"
            if job.settings:
                res = job.settings.get('resolution','1080p')
                cod = job.settings.get('codec','libx264')
                cfg_str = f"{res} | {cod}"
            
            text += (
                f"\n<b>{i}. <code>{job.final_name}</code></b>\n"
                f"   ├ <b>Mode:</b> {job.mode.upper()}\n"
                f"   ├ <b>Set:</b> {cfg_str}\n"
                f"   └ <b>ID:</b> <code>{job.job_id}</code>\n"
            )
    return text

@Client.on_message(filters.command('status') & check_user & filters.private)
async def check_status(client, message):
    try: await message.delete() # CLEANUP
    except: pass
    text = build_status_text()
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh Status", callback_data="refresh_status")]])
    await message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)

@Client.on_callback_query(filters.regex('^refresh_status$'))
async def refresh_status_cb(client, cq):
    text = build_status_text()
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh Status", callback_data="refresh_status")]])
    try: await cq.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" in str(e):
            await cq.answer("Status is already up to date!", show_alert=False)
        else:
            await cq.answer("Error refreshing.", show_alert=True)

@Client.on_message(filters.command('cancel') & check_user & filters.private)
async def cancel_job(client, message):
    try: await message.delete() # CLEANUP
    except: pass
    if len(message.command) != 2:
        return await message.reply_text("Usage: /cancel <job_id>", parse_mode=ParseMode.HTML)
    target = message.command[1]

    removed = False
    temp_q  = asyncio.Queue()
    while not job_queue.empty():
        job = await job_queue.get()
        if job.job_id == target:
            removed = True
            await job.status_msg.edit(f"❌ Job <code>{target}</code> cancelled before start.", parse_mode=ParseMode.HTML)
        else:
            await temp_q.put(job)
        job_queue.task_done()
    while not temp_q.empty():
        await job_queue.put(await temp_q.get())

    if removed: return

    entry = running_jobs.get(target)
    if not entry:
        return await message.reply_text(f"No job `<code>{target}</code>` found.", parse_mode=ParseMode.HTML)

    entry['proc'].kill()
    for t in entry['tasks']: t.cancel()
    running_jobs.pop(target, None)
    await message.reply_text(f"🛑 Job `<code>{target}</code>` aborted.", parse_mode=ParseMode.HTML)

# --------------------- WORKER ---------------------

async def queue_worker(client: Client):
    logger.info("Queue worker started.")
    while True:
        job = await job_queue.get()
        job_start_time = time.time() # Track total time

        try:
            await job.status_msg.edit(
                f"▶️ Starting <code>{job.job_id}</code> ({job.mode})…\n"
                f"Use <code>/cancel {job.job_id}</code> to abort.",
                parse_mode=ParseMode.HTML
            )

            if job.mode == 'soft': out_file = await softmux_vid(job.vid, job.sub, job.status_msg, job.job_id, job.final_name)
            elif job.mode == 'hard': out_file = await hardmux_vid(job.vid, job.sub, job.status_msg, job.job_id, job.settings, job.final_name)
            else: out_file = await nosub_encode(job.vid, job.status_msg, job.job_id, job.settings, job.final_name)

            if out_file:
                src = os.path.join(Config.DOWNLOAD_DIR, out_file)
                dst = os.path.join(Config.DOWNLOAD_DIR, job.final_name)
                try: os.rename(src, dst)
                except Exception: dst = src

                # 🟢 AUTO-SPLIT AND UPLOAD LOOP
                parts = await split_video(dst, job.status_msg)
                total_elapsed = time.time() - job_start_time
                
                for i, part_path in enumerate(parts):
                    # Name logic: add "Part 1", "Part 2" only if split occurred
                    if len(parts) > 1:
                        display_name = f"{os.path.splitext(job.final_name)[0]} - Part {i+1}{os.path.splitext(job.final_name)[1]}"
                        await job.status_msg.edit(f"📤 <b>Uploading Part {i+1} of {len(parts)}...</b>", parse_mode=ParseMode.HTML)
                    else:
                        display_name = job.final_name

                    thumb_path = await generate_thumbnail(part_path)
                    t0 = time.time()
                    
                    sent_msg = None
                    try:
                        sent_msg = await client.send_document(
                            job.chat_id, document=part_path, caption=display_name, file_name=display_name,
                            thumb=thumb_path, force_document=True, progress=progress_bar,
                            progress_args=(f'📤 Uploading Part {i+1}…' if len(parts)>1 else '📤 Uploading…', job.status_msg, t0, job.job_id)
                        )
                    except Exception as e:
                        logger.error(f"Failed to upload {display_name}: {e}")
                        await job.status_msg.edit(f"❌ Upload failed for {display_name}: {e}")

                    if thumb_path and os.path.exists(thumb_path):
                        os.remove(thumb_path)

                    # Forwarding to Channel via API Request
                    if sent_msg and hasattr(Config, 'DEST_CHANNEL') and Config.DEST_CHANNEL:
                        try:
                            import requests
                            api_url = f"https://api.telegram.org/bot{Config.BOT_TOKEN}/sendDocument"
                            payload = {
                                "chat_id": Config.DEST_CHANNEL,
                                "document": sent_msg.document.file_id,
                                "caption": display_name
                            }
                            requests.post(api_url, data=payload)
                        except Exception as e:
                            logger.error(f"Failed to send to channel via API: {e}")
                            
                    # Clean up the partial file (if it was split)
                    if part_path != dst:
                        try: os.remove(part_path)
                        except: pass

                # Final Success Message
                final_size = os.path.getsize(dst)
                part_text = f" (in {len(parts)} parts)" if len(parts) > 1 else ""
                
                prof_msg = (
                    f"✅ <b>Encoding Completed Successfully!</b>\n\n"
                    f"🎬 <b>File:</b> <code>{job.final_name}</code>{part_text}\n"
                    f"🆔 <b>Job ID:</b> <code>{job.job_id}</code>\n"
                    f"⏱️ <b>Time Taken:</b> {_fmt_time(total_elapsed)}\n"
                    f"💾 <b>Total Size:</b> {_humanbytes(final_size)}\n"
                )
                await job.status_msg.edit(prof_msg, parse_mode=ParseMode.HTML)

                # Cleanup original files
                for fn in (job.vid, job.sub, job.final_name):
                    try:
                        if fn: os.remove(os.path.join(Config.DOWNLOAD_DIR, fn))
                    except: pass

        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        finally:
            job_queue.task_done()

@Client.on_message(filters.command('restart') & check_user & filters.private)
async def restart_bot(client, message):
    try: await message.delete() # CLEANUP
    except: pass
    await message.reply_text("♻️ Your All tasks and settings are now reset ✅")
    try:
        for jid, entry in list(running_jobs.items()):
            try: entry['proc'].kill()
            except: pass
            for t in entry.get('tasks', []):
                try: t.cancel()
                except: pass
            running_jobs.pop(jid, None)
    except: pass
    try:
        conn = sqlite3.connect('muxdb.sqlite', check_same_thread=False)
        conn.execute('DELETE FROM muxbot;')
        conn.commit()
        conn.close()
    except: pass
    await asyncio.sleep(1)
    os.execv(sys.executable, [sys.executable] + sys.argv)



@Client.on_message(filters.command('cleanup') & check_user & filters.private)
async def cleanup_server(client, message):
    logger.info("/cleanup by %s", message.from_user.id)
    
    # 1. Safety Check: Don't delete files if jobs are actively running!
    if running_jobs or not job_queue.empty():
        return await message.reply_text(
            "⚠️ **Warning:** There are active or queued encoding jobs!\n"
            "Please wait for them to finish before running a cleanup.",
            parse_mode=ParseMode.MARKDOWN
        )

    try: await message.delete() # Clean up the user's command
    except: pass
    
    status = await message.reply_text("🧹 **Scanning and cleaning server storage...**", parse_mode=ParseMode.MARKDOWN)
    
    deleted_count = 0
    freed_space = 0
    
    # 2. Iterate through the downloads directory
    for file in os.listdir(Config.DOWNLOAD_DIR):
        # Protect the user settings file from being deleted!
        if file == "user_settings.json":
            continue
            
        filepath = os.path.join(Config.DOWNLOAD_DIR, file)
        
        if os.path.isfile(filepath):
            freed_space += os.path.getsize(filepath)
            try:
                os.remove(filepath)
                deleted_count += 1
            except Exception as e:
                logger.error(f"Failed to delete {file}: {e}")

    # 3. Clean up the logs directory too
    log_dir = "logs"
    log_count = 0
    if os.path.exists(log_dir):
        for log_file in os.listdir(log_dir):
            if log_file.endswith(".log") and log_file != "bot.log": # Keep main bot log if it exists
                log_path = os.path.join(log_dir, log_file)
                freed_space += os.path.getsize(log_path)
                try:
                    os.remove(log_path)
                    log_count += 1
                except: pass

    await status.edit(
        f"✅ **Server Cleanup Complete!**\n\n"
        f"🗑 **Orphaned Files Removed:** `{deleted_count}`\n"
        f"📜 **Old Logs Cleared:** `{log_count}`\n"
        f"💾 **Total Space Freed:** `{_humanbytes(freed_space)}`",
        parse_mode=ParseMode.MARKDOWN
    )
