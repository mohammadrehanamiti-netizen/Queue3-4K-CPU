# helper_func/queue.py

import asyncio
from typing import NamedTuple
from pyrogram.types import Message

class Job(NamedTuple):
    job_id: str         # unique short ID
    mode: str           # "soft" or "hard" or "nosub"
    chat_id: int
    vid: str            # input video filename
    sub: str            # input subtitle filename
    final_name: str     # the filename to rename→upload
    status_msg: Message # the message we’ll keep editing for progress
    settings: dict      # <-- ADDED: Snapshot of user settings for this specific job

job_queue: asyncio.Queue[Job] = asyncio.Queue()
