"""
Telegram bot for the E-Ticket system.
=====================================
Sends a customer's e-ticket to their Telegram chat when they send their
phone number or @username, or scan the unique QR code printed on their ticket.

Two ways the bot receives messages:
  * Webhook  — Telegram pushes updates to POST /telegram/webhook (app.py)
  * Polling  — telegram_polling.py calls getUpdates in a loop (local testing)

The two are mutually exclusive in Telegram: setWebhook and getUpdates cannot
run at the same time.

Config (env vars):
  TELEGRAM_BOT_TOKEN        required — from @BotFather
  TELEGRAM_BOT_USERNAME     optional — override, else resolved via getMe
  TELEGRAM_WEBHOOK_SECRET   required for webhook mode — random string
  PUBLIC_URL                optional — public base URL, else derived per request
  TELEGRAM_ALLOWED_CHAT_IDS optional — comma-separated allowlist, empty = anyone
  TELEGRAM_DRY_RUN          optional — "1" logs instead of sending
"""

import io
import os
import re
import time
from datetime import datetime

import requests

from ticket_core import generate_ticket, load_records, update_record

# -------------------- SETTINGS --------------------

BOT_TOKEN     = os.environ.get("TELEGRAM_BOT_TOKEN", "")
BOT_USERNAME  = os.environ.get("TELEGRAM_BOT_USERNAME", "").lstrip("@")
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
PUBLIC_URL    = os.environ.get("PUBLIC_URL", "").rstrip("/")
DRY_RUN       = os.environ.get("TELEGRAM_DRY_RUN", "") == "1"

API_BASE = "https://api.telegram.org"
TIMEOUT  = 30

# Per-chat lookup throttle: max LOOKUP_LIMIT lookups per LOOKUP_WINDOW seconds.
LOOKUP_LIMIT  = 10
LOOKUP_WINDOW = 300
_lookups: dict[str, list[float]] = {}

_username_cache: tuple[str, float] | None = None
# --------------------------------------------------


class TelegramError(Exception):
    """Raised when the Telegram API returns ok=false."""


# ==================== API CLIENT ====================

def _api(method: str, **params):
    """Call a Telegram Bot API method with query-string params."""
    if not BOT_TOKEN:
        raise TelegramError("TELEGRAM_BOT_TOKEN is not set")
    resp = requests.get(f"{API_BASE}/bot{BOT_TOKEN}/{method}", params=params, timeout=TIMEOUT)
    data = resp.json()
    if not data.get("ok"):
        raise TelegramError(data.get("description", "Telegram API error"))
    return data["result"]


def get_me() -> dict:
    """Bot identity. Used to resolve the @username for QR deep links."""
    return _api("getMe")


def bot_username() -> str:
    """@username of the bot, resolved once and cached."""
    global _username_cache
    if BOT_USERNAME:
        return BOT_USERNAME
    if _username_cache and time.time() - _username_cache[1] < 3600:
        return _username_cache[0]
    username = get_me().get("username", "")
    if username:
        _username_cache = (username, time.time())
    return username


def set_webhook(url: str, secret: str) -> dict:
    """Point Telegram at our webhook endpoint."""
    return _api("setWebhook", url=url, secret_token=secret,
                allowed_updates=["message"], drop_pending_updates="true")


def delete_webhook() -> dict:
    """Remove the webhook so getUpdates polling can take over."""
    return _api("deleteWebhook", drop_pending_updates="true")


def get_webhook_info() -> dict:
    return _api("getWebhookInfo")


def send_message(chat_id: str, text: str) -> dict:
    """Send a plain text message."""
    if DRY_RUN:
        print(f"[dry-run] sendMessage -> {chat_id}: {text}")
        return {"ok": True, "dry_run": True}
    resp = requests.post(
        f"{API_BASE}/bot{BOT_TOKEN}/sendMessage",
        data={"chat_id": str(chat_id), "text": text, "parse_mode": "HTML"},
        timeout=TIMEOUT,
    )
    data = resp.json()
    if not data.get("ok"):
        raise TelegramError(data.get("description", "sendMessage failed"))
    return data["result"]


def send_photo(chat_id: str, photo: bytes, filename: str = "ticket.jpg",
               caption: str = "") -> dict:
    """Send a JPEG as a photo (multipart upload)."""
    if DRY_RUN:
        print(f"[dry-run] sendPhoto -> {chat_id}: {filename} ({len(photo)} bytes) {caption}")
        return {"ok": True, "dry_run": True}
    payload = {"chat_id": str(chat_id)}
    if caption:
        payload["caption"] = caption
    resp = requests.post(
        f"{API_BASE}/bot{BOT_TOKEN}/sendPhoto",
        data=payload,
        files={"photo": (filename, photo, "image/jpeg")},
        timeout=TIMEOUT,
    )
    data = resp.json()
    if not data.get("ok"):
        raise TelegramError(data.get("description", "sendPhoto failed"))
    return data["result"]


# ==================== QR CODES ====================

def deep_link(qr_token: str) -> str:
    """Telegram deep link that opens the bot with this ticket's token."""
    return f"https://t.me/{bot_username()}?start={qr_token}"


def qr_png(qr_token: str) -> io.BytesIO:
    """Render the ticket's QR code as a PNG."""
    import qrcode

    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(deep_link(qr_token))
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


# ==================== RECORD LOOKUP ====================

def _digits(text: str) -> str:
    return re.sub(r"\D", "", text or "")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def find_records_by_contact(contact: str) -> list:
    """Match a contact string (phone number or @username) against records.

    Strongest match first: exact phone, then phone digits, then Telegram
    username, then customer name.
    """
    contact     = (contact or "").strip()
    norm        = _norm(contact)
    digits      = _digits(contact)
    at_username = norm.lstrip("@")

    records = load_records()
    if not records:
        return []

    # 1. Exact phone match
    hits = [r for r in records if r.get("phone") and _norm(r["phone"]) == norm]
    if hits:
        return hits

    # 2. Phone digits match (ignores +, spaces, dashes)
    if len(digits) >= 6:
        hits = [r for r in records if r.get("phone") and _digits(r["phone"]) == digits]
        if hits:
            return hits

    # 3. Telegram @username match
    if at_username:
        hits = [r for r in records
                if r.get("telegram_username")
                and _norm(r["telegram_username"]).lstrip("@") == at_username]
        if hits:
            return hits

    # 4. Customer name match (exact, then word-level)
    hits = [r for r in records if r.get("customer_name") and _norm(r["customer_name"]) == norm]
    if hits:
        return hits

    # Word match — "rayu" finds "Mr. Rayu", "mr rayu" also finds it
    input_words = {w.strip(".,") for w in norm.split()}
    hits = []
    for r in records:
        if not r.get("customer_name"):
            continue
        name_words = {w.strip(".,") for w in re.findall(r"[\w@.+-]+", _norm(r["customer_name"]))}
        if input_words & name_words:
            hits.append(r)
    if hits:
        return hits

    # Substring match, last resort
    return [r for r in records
            if r.get("customer_name") and norm in _norm(r["customer_name"])]


def find_record_by_token(qr_token: str) -> dict | None:
    """Find the record a QR deep link points at."""
    if not qr_token:
        return None
    for r in load_records():
        if r.get("qr_token") == qr_token:
            return r
    return None


def _too_many_lookups(chat_id: str) -> bool:
    now = time.time()
    hits = [t for t in _lookups.get(chat_id, []) if now - t < LOOKUP_WINDOW]
    if len(hits) >= LOOKUP_LIMIT:
        _lookups[chat_id] = hits
        return True
    hits.append(now)
    _lookups[chat_id] = hits
    return False


# ==================== MESSAGES ====================

WELCOME = (
    "🎫 <b>Rayucode E-Ticket</b>\n\n"
    "Send me your <b>phone number</b> or <b>@username</b> and I'll send your "
    "e-ticket. You can also scan the QR code on your voucher.\n\n"
    "Type /help for the options."
)

HELP = (
    "How to get your e-ticket:\n"
    "• Send your <b>phone number</b> or <b>@username</b>\n"
    "• Or scan the QR code printed on your voucher\n\n"
    "Your ticket is sent as a photo — save it to your phone."
)


def ticket_caption(record: dict) -> str:
    """Caption shown under the ticket photo."""
    lines = ["🎫 <b>Your E-Ticket</b>", ""]
    lines.append(f"Code: <b>{record.get('promo_code', '')}</b>")
    if record.get("discount") is not None:
        lines.append(f"Discount: <b>{record['discount']:g}%</b>")
    if record.get("customer_name"):
        lines.append(f"Issued to: {record['customer_name']}")
    lines.append("")
    lines.append("✅ Saved — show this at the counter.")
    return "\n".join(lines)


def send_ticket_to_chat(chat_id: str, record: dict) -> bool:
    """Render the record's ticket and send it to a chat. Returns success."""
    photo = generate_ticket(
        record.get("promo_code", ""),
        record.get("customer_name") or "",
    ).getvalue()
    send_photo(chat_id, photo, filename=f"ticket_{record.get('promo_code', 'e')}.jpg",
               caption=ticket_caption(record))
    return True


def _remember_chat(record: dict, chat_id: str, chat_name: str,
                   username: str = "") -> None:
    """Store the customer's Telegram chat on their record."""
    if DRY_RUN:
        return
    fields = {
        "telegram_chat_id": str(chat_id),
        "telegram_chat_name": chat_name,
        "telegram_sent_at": datetime.now().isoformat(timespec="seconds"),
    }
    if username:
        fields["telegram_username"] = username
    update_record(record["id"], **fields)


# ==================== UPDATE HANDLER ====================

def handle_update(update: dict) -> str:
    """Handle one Telegram update. Returns a short log line."""
    message = update.get("message") or update.get("edited_message")
    if not message:
        return "ignored: no message"

    chat       = message.get("chat") or {}
    chat_id    = chat.get("id")
    chat_name  = " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
    username   = (chat.get("username") or "")
    text       = (message.get("text") or "").strip()

    allowed = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
    if allowed:
        allowed_ids = {x.strip() for x in allowed.split(",") if x.strip()}
        if str(chat_id) not in allowed_ids:
            send_message(chat_id, "⛔ This bot is restricted.")
            return f"denied: {chat_id}"

    if not text:
        send_message(chat_id, "Please send your phone number or @username as text, "
                              "or scan the QR code on your voucher.")
        return f"non-text from {chat_id}"

    # ── /start with a QR token ──
    if text.startswith("/start"):
        parts   = text.split(maxsplit=1)
        token   = parts[1].strip() if len(parts) > 1 else ""
        record  = find_record_by_token(token) if token else None

        if not token:
            send_message(chat_id, WELCOME)
            return f"/start from {chat_id}"

        if not record:
            send_message(chat_id, "❌ This QR code isn't valid any more. "
                                  "Please ask staff for a new one.")
            return f"/start bad token from {chat_id}"

        send_ticket_to_chat(chat_id, record)
        _remember_chat(record, chat_id, chat_name or username, username)
        return f"sent ticket {record.get('promo_code')} to {chat_id}"

    # ── /help ──
    if text.startswith("/help"):
        send_message(chat_id, HELP)
        return f"/help from {chat_id}"

    # ── The customer typed their promo code (disambiguating multiple hits) ──
    records = load_records()
    by_code = [r for r in records
               if _norm(r.get("promo_code", "")) == _norm(text)]
    if by_code and len(text) >= 4:
        record = by_code[0]
        send_ticket_to_chat(chat_id, record)
        _remember_chat(record, chat_id, chat_name or username, username)
        return f"sent ticket by code {record.get('promo_code')} to {chat_id}"

    # ── Name / phone / @username lookup ──
    if _too_many_lookups(chat_id):
        send_message(chat_id, "⏳ Too many lookups. Please wait a few minutes.")
        return f"rate limited {chat_id}"

    hits = find_records_by_contact(text)
    if not hits:
        send_message(chat_id,
                     f"❌ I couldn't find a ticket for <b>{text}</b>.\n\n"
                     "Please check the spelling, or scan the QR code on your voucher.")
        return f"no match for {text!r} from {chat_id}"

    if len(hits) == 1:
        record = hits[0]
        send_ticket_to_chat(chat_id, record)
        _remember_chat(record, chat_id, chat_name or username, username)
        return f"sent ticket {record.get('promo_code')} to {chat_id}"

    # Several tickets — ask for the promo code
    lines = [f"🔎 I found <b>{len(hits)}</b> tickets matching <b>{text}</b>.",
             "", "Please send the promo code of the one you want:"]
    for r in hits[:10]:
        lines.append(f"• <code>{r.get('promo_code', '')}</code> — "
                     f"{r.get('customer_name') or 'no name'} "
                     f"({r.get('discount', 0):g}%)")
    send_message(chat_id, "\n".join(lines))
    return f"{len(hits)} matches for {text!r} from {chat_id}"
