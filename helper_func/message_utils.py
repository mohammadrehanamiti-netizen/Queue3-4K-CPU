import logging
import time

from pyrogram.errors import FloodWait

logger = logging.getLogger("telegram.edit")

_EDIT_STATE = {}


def _message_key(message):
    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None)
    message_id = getattr(message, "id", None)
    if chat_id is None or message_id is None:
        return None
    return chat_id, message_id


def _reply_markup_fingerprint(reply_markup):
    if reply_markup is None:
        return None

    inline_keyboard = getattr(reply_markup, "inline_keyboard", None)
    if inline_keyboard is None:
        return repr(reply_markup)

    rows = []
    for row in inline_keyboard:
        rows.append(
            tuple(
                (
                    getattr(button, "text", None),
                    getattr(button, "callback_data", None),
                    getattr(button, "url", None),
                )
                for button in row
            )
        )
    return tuple(rows)


async def safe_edit_message(
    message,
    text,
    *,
    parse_mode=None,
    reply_markup=None,
    min_interval=8.0,
    force=False,
):
    key = _message_key(message)
    if key is None:
        try:
            await message.edit(text=text, parse_mode=parse_mode, reply_markup=reply_markup)
            return True
        except Exception:
            return False

    state = _EDIT_STATE.setdefault(
        key,
        {
            "last_edit": 0.0,
            "fingerprint": None,
            "flood_until": 0.0,
        },
    )

    now = time.monotonic()
    fingerprint = (text, str(parse_mode), _reply_markup_fingerprint(reply_markup))

    if state["fingerprint"] == fingerprint:
        return False

    if state["flood_until"] > now:
        return False

    if not force and (now - state["last_edit"]) < min_interval:
        return False

    try:
        await message.edit(text=text, parse_mode=parse_mode, reply_markup=reply_markup)
        state["last_edit"] = time.monotonic()
        state["fingerprint"] = fingerprint
        state["flood_until"] = 0.0
        return True
    except FloodWait as exc:
        wait_seconds = int(getattr(exc, "value", 0) or 0)
        state["flood_until"] = time.monotonic() + wait_seconds + 1
        logger.warning(
            "FloodWait while editing chat_id=%s message_id=%s; backing off for %ss",
            key[0],
            key[1],
            wait_seconds,
        )
        return False
    except Exception as exc:
        if "MESSAGE_NOT_MODIFIED" in str(exc):
            state["fingerprint"] = fingerprint
            return False
        logger.debug("Ignoring edit failure for %s: %s", key, exc)
        return False
