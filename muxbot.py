import logging, os, sys
# --- TIME FIX HACK ---
# --- BULLETPROOF TIME FIX HACK ---
import time, requests
from email.utils import parsedate_to_datetime

try:
    print("Checking Telegram's official clock...")
    res = requests.head("https://api.telegram.org", timeout=5)
    tg_date_str = res.headers.get("Date")
    
    if tg_date_str:
        true_time = parsedate_to_datetime(tg_date_str).timestamp()
        server_time = time.time()
        time_offset = true_time - server_time
        
        if abs(time_offset) > 5:
            print(f"Fixing server clock! Offset applied: {time_offset:.0f} seconds.")
            
            # Patch standard time
            orig_time = time.time
            time.time = lambda: orig_time() + time_offset
            
            # Patch nanosecond time
            if hasattr(time, 'time_ns'):
                orig_time_ns = time.time_ns
                time.time_ns = lambda: int(orig_time_ns() + (time_offset * 1e9))
        else:
            print("Server time is already accurate enough!")
except Exception as e:
    print(f"WARNING: Time sync hack failed: {e}")
# ---------------------------------


from logging.handlers import RotatingFileHandler
from config import Config
from helper_func.dbhelper import Database as Db
from plugins.muxer import queue_worker

os.makedirs("logs", exist_ok=True)

log_fmt = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
logger = logging.getLogger()
logger.setLevel(logging.INFO)

from logging.handlers import RotatingFileHandler
fh = RotatingFileHandler("logs/bot.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
fh.setFormatter(logging.Formatter(log_fmt))
fh.setLevel(logging.INFO)

ch = logging.StreamHandler()
ch.setFormatter(logging.Formatter(log_fmt))
ch.setLevel(logging.INFO)

logger.handlers.clear()
logger.addHandler(fh)
logger.addHandler(ch)

logging.getLogger("pyrogram").setLevel(logging.INFO)
logging.getLogger("asyncio").setLevel(logging.WARNING)

def _uncaught(exc_type, exc, tb):
    logging.getLogger("bot").error("Uncaught exception", exc_info=(exc_type, exc, tb))
sys.excepthook = _uncaught

db = Db().setup()
if not os.path.isdir(Config.DOWNLOAD_DIR):
    os.mkdir(Config.DOWNLOAD_DIR)

from pyrogram import Client
class QueueBot(Client):
    async def start(self):
        await super().start()
        # launch our single background worker
        self.loop.create_task(queue_worker(self))

        # --- CACHE THE DESTINATION CHANNEL ---
        try:
            if hasattr(Config, 'DEST_INVITE_LINK') and Config.DEST_INVITE_LINK:
                logging.getLogger("bot").info("Caching destination channel ID...")
                await self.get_chat(Config.DEST_INVITE_LINK)
                logging.getLogger("bot").info("Channel cached successfully! Ready to forward.")
        except Exception as e:
            logging.getLogger("bot").error(f"Failed to cache channel: {e}")
        # -------------------------------------

app = QueueBot(
    "SubtitleMuxer",
    bot_token=Config.BOT_TOKEN,
    api_id=Config.APP_ID,
    api_hash=Config.API_HASH,
    plugins=dict(root="plugins")
)

if __name__ == "__main__":
    print("Starting the bot...")
    logger.info("Starting the bot...")
    try:
        app.run()
    except Exception as e:
        print(f"CRASHED: {e}")
    print("Bot has stopped.")
