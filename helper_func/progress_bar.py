# helper_func/progress_bar.py

import time

from pyrogram.enums import ParseMode

from helper_func.message_utils import safe_edit_message


async def progress_bar(current, total, text, message, start, job_id=None):
    """
    Shared progress callback for Telegram downloads/uploads.
    It intentionally throttles edits hard to avoid FloodWait errors.
    """
    if current <= 0:
        return

    diff = max(time.time() - start, 0.0)
    has_total = bool(total and total > 0)
    is_final = has_total and current >= total
    speed = current / diff if diff > 0 else 0
    elapsed_ms = round(diff * 1000)

    card = f"<b>{text}</b>\n\n"
    if job_id:
        card += f"Job ID: <code>{job_id}</code>\n\n"

    if has_total:
        percentage = min(100.0, (current * 100) / total)
        eta_ms = int(((total - current) / speed) * 1000) if speed > 0 else 0
        total_blocks = 15
        filled_blocks = min(total_blocks, int((percentage / 100) * total_blocks))
        bar = "#" * filled_blocks + "-" * (total_blocks - filled_blocks)

        card += (
            f"Progress: [{bar}] <b>{percentage:.1f}%</b>\n\n"
            f"Size: {_humanbytes(current)} / {_humanbytes(total)}\n"
            f"Speed: {_humanbytes(speed)}/s\n"
            f"ETA: {TimeFormatter(elapsed_ms + eta_ms)}"
        )
    else:
        card += (
            f"Transferred: {_humanbytes(current)}\n"
            f"Speed: {_humanbytes(speed)}/s\n"
            f"Elapsed: {TimeFormatter(elapsed_ms)}"
        )

    await safe_edit_message(
        message,
        card,
        parse_mode=ParseMode.HTML,
        min_interval=8.0,
        force=is_final,
    )


def _humanbytes(size):
    """Convert bytes -> human-readable string."""
    if not size:
        return "0 B"
    power = 2**10
    n = 0
    units = ["B", "KB", "MB", "GB", "TB"]
    while size >= power and n < len(units) - 1:
        size /= power
        n += 1
    return f"{round(size, 2)} {units[n]}"


def TimeFormatter(milliseconds: int) -> str:
    """Convert milliseconds -> human-readable time."""
    seconds, _ = divmod(int(milliseconds), 1000)
    minutes, sec = divmod(seconds, 60)
    hours, min_ = divmod(minutes, 60)
    days, hr = divmod(hours, 24)

    parts = []
    if days:
        parts.append(f"{days}d")
    if hr:
        parts.append(f"{hr}h")
    if min_:
        parts.append(f"{min_}m")
    if sec:
        parts.append(f"{sec}s")

    return " ".join(parts) if parts else "0s"
