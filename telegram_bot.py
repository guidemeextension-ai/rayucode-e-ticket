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
  TELEGRAM_BACKUP_USERNAMES optional — default President_Alien,SassdahRAK;
                           blank disables ticket backups
"""

import html
import io
import os
import re
import time
from datetime import datetime

import requests

import record_store
from ticket_core import generate_ticket, load_records, update_record

# -------------------- SETTINGS --------------------

BOT_TOKEN     = os.environ.get("TELEGRAM_BOT_TOKEN", "")
BOT_USERNAME  = os.environ.get("TELEGRAM_BOT_USERNAME", "").lstrip("@")
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
PUBLIC_URL    = os.environ.get("PUBLIC_URL", "").rstrip("/")
DRY_RUN       = os.environ.get("TELEGRAM_DRY_RUN", "") == "1"

API_BASE = "https://api.telegram.org"
CONNECT_TIMEOUT = 5
TIMEOUT = 15
UPLOAD_CONNECT_TIMEOUT = 3
UPLOAD_TIMEOUT = 8
BACKUP_BUDGET = 48
BACKUP_CONTACT_BUDGET = 13
POLL_TIMEOUT_MARGIN = 5

# Per-chat lookup throttle: max LOOKUP_LIMIT lookups per LOOKUP_WINDOW seconds.
LOOKUP_LIMIT  = 10
LOOKUP_WINDOW = 300
# Hard cap on tracked chats so unique chat ids cannot grow memory unbounded.
MAX_LOOKUP_CHATS = 4096
_lookups: dict = {}

_username_cache: tuple[str, float] | None = None
# --------------------------------------------------


class TelegramError(Exception):
    """Sanitized Telegram transport, response, or configuration failure."""


# ==================== API CLIENT ====================

def _request(method: str, read_timeout: int, **kwargs):
    """Make one request; never expose token URLs or untrusted response bodies."""
    if not BOT_TOKEN:
        raise TelegramError("TELEGRAM_BOT_TOKEN is not set")
    try:
        resp = requests.post(
            f"{API_BASE}/bot{BOT_TOKEN}/{method}",
            timeout=(UPLOAD_CONNECT_TIMEOUT if method == "sendPhoto" else CONNECT_TIMEOUT,
                     read_timeout),
            allow_redirects=False,
            **kwargs,
        )
        resp.raise_for_status()
    except requests.RequestException:
        raise TelegramError("Telegram request failed") from None
    try:
        data = resp.json()
    except ValueError:
        raise TelegramError("Telegram returned invalid JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
        raise TelegramError("Telegram returned an invalid response")
    if not data["ok"]:
        raise TelegramError("Telegram API rejected the request")
    if "result" not in data:
        raise TelegramError("Telegram returned an invalid response")
    return data["result"]


def _api(method: str, **params):
    """POST JSON, preserving booleans/lists and allowing long-poll waits."""
    read_timeout = TIMEOUT
    if method == "getUpdates":
        try:
            poll_timeout = int(params.get("timeout", 0))
        except (TypeError, ValueError, OverflowError):
            raise TelegramError("Invalid polling timeout") from None
        if not 0 <= poll_timeout <= 50:
            raise TelegramError("Polling timeout must be between 0 and 50 seconds")
        read_timeout = max(TIMEOUT, poll_timeout + POLL_TIMEOUT_MARGIN)
    return _request(method, read_timeout, json=params)


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
                allowed_updates=["message"], drop_pending_updates=False)


def delete_webhook() -> dict:
    """Remove the webhook so getUpdates polling can take over."""
    return _api("deleteWebhook", drop_pending_updates=False)


def get_webhook_info() -> dict:
    return _api("getWebhookInfo")


def send_message(chat_id: str, text: str) -> dict:
    """Send a plain text message."""
    if DRY_RUN:
        print(f"[dry-run] sendMessage -> {chat_id}: {text}")
        return {"ok": True, "dry_run": True}
    return _api("sendMessage", chat_id=str(chat_id), text=text, parse_mode="HTML")


def send_photo(chat_id: str, photo: bytes, filename: str = "ticket.jpg",
               caption: str = "", parse_mode: str | None = None) -> dict:
    """Send a JPEG in one bounded attempt; retries risk duplicate delivery."""
    if DRY_RUN:
        print(f"[dry-run] sendPhoto -> {chat_id}: {filename} ({len(photo)} bytes) {caption}")
        return {"ok": True, "dry_run": True}
    payload = {"chat_id": str(chat_id)}
    if caption:
        payload["caption"] = caption
    if parse_mode is not None:
        payload["parse_mode"] = parse_mode

    return _request(
        "sendPhoto", UPLOAD_TIMEOUT, data=payload,
        files={"photo": (filename, photo, "image/jpeg")},
    )


def send_ticket_backups(record: dict, *, deadline: float | None = None) -> list[dict]:
    """Send ticket-only backups to configured, previously observed private chats.

    No customer delivery metadata is changed. Each recipient is independent;
    dry-run is reported as unavailable, never as an actual delivery. The
    optional absolute monotonic deadline is capped by a local 48-second budget.
    Requests timeouts are inactivity bounds, not strict wall-clock guarantees.
    """
    configured = os.environ.get(
        "TELEGRAM_BACKUP_USERNAMES", "President_Alien,SassdahRAK"
    )
    usernames = []
    seen = set()
    for value in configured.split(","):
        username = value.strip().lstrip("@")
        if username and username.casefold() not in seen:
            usernames.append(username)
            seen.add(username.casefold())
    results = []
    local_deadline = time.monotonic() + BACKUP_BUDGET
    deadline = local_deadline if deadline is None else min(local_deadline, deadline)
    photo = None
    # Plain text, no parse_mode. Bound each field in UTF-16 units, which
    # Telegram uses for caption limits, including astral Unicode characters.
    def field(value):
        text = str(value if value is not None else "")
        return text.encode("utf-16-le", errors="replace")[:240].decode(
            "utf-16-le", errors="ignore"
        )

    caption = "Ticket creation backup\n" + "\n".join(
        f"{label}: {field(record.get(key))}"
        for label, key in (
            ("Code", "promo_code"), ("Customer", "customer_name"),
            ("Phone", "phone"), ("Discount (%)", "discount"),
            ("Created at", "created_at"),
        )
    )
    for username in usernames:
        result = {"username": username, "status": "failed"}
        results.append(result)
        if DRY_RUN:
            result.update(status="unavailable", error="Dry-run: backup not sent")
            continue
        try:
            if deadline - time.monotonic() < (
                BACKUP_CONTACT_BUDGET + UPLOAD_CONNECT_TIMEOUT + UPLOAD_TIMEOUT
            ):
                result.update(status="unavailable", error="Backup time budget exhausted")
                continue
            chat_id = record_store.find_contact(username)
            if not chat_id:
                result.update(status="unavailable", error="No bot-known private contact")
                continue
            if photo is None:
                photo = generate_ticket(
                    record.get("promo_code", ""), record.get("customer_name") or ""
                ).getvalue()
            if deadline - time.monotonic() < UPLOAD_CONNECT_TIMEOUT + UPLOAD_TIMEOUT:
                result.update(status="unavailable", error="Backup time budget exhausted")
                continue
            sent = send_photo(chat_id, photo, filename="ticket-backup.jpg", caption=caption)
            if isinstance(sent, dict) and sent.get("dry_run"):
                result.update(status="unavailable", error="Dry-run: backup not sent")
            else:
                result["status"] = "sent"
        except Exception:
            # Storage/renderer errors may contain credentials too: never echo them.
            result["error"] = "Ticket backup failed"
    return results


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


def bot_qr_png() -> io.BytesIO:
    """Render a QR code that opens the bot in Telegram when scanned."""
    import qrcode

    link = f"https://t.me/{bot_username()}"
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(link)
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
    """Match a contact string against records.

    Strongest match first: exact phone, then phone digits, then Telegram
    username, then customer name, then promo code.
    """
    contact     = (contact or "").strip()
    norm        = _norm(contact)
    digits      = _digits(contact)
    at_username = norm.lstrip("@")

    records = load_records()
    if not records:
        return []

    # 0. Exact promo code match — the strongest possible signal
    if len(norm) >= 4:
        hits = [r for r in records if _norm(r.get("promo_code", "")) == norm]
        if hits:
            return hits

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
    # Bound memory: drop chats whose whole window has gone stale, then hard-cap
    # the table by evicting the least recently active chat.
    if len(_lookups) > MAX_LOOKUP_CHATS:
        for key in [k for k, hits in _lookups.items()
                    if not any(now - t < LOOKUP_WINDOW for t in hits)]:
            del _lookups[key]
    hits = [t for t in _lookups.get(chat_id, []) if now - t < LOOKUP_WINDOW]
    if len(hits) >= LOOKUP_LIMIT:
        _lookups[chat_id] = hits
        return True
    if chat_id not in _lookups and len(_lookups) >= MAX_LOOKUP_CHATS:
        oldest = min(_lookups, key=lambda k: _lookups[k][-1] if _lookups[k] else 0)
        del _lookups[oldest]
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

RATE_LIMITED = "⏳ Too many lookups. Please wait a few minutes."


def _discount_text(value) -> str:
    """Format a discount for an HTML caption without ever raising."""
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return html.escape(str(value))[:32]


def _safe_filename(value) -> str:
    """Strip characters that could break multipart headers."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "")).strip("._-")[:64] or "ticket"


def ticket_caption(record: dict) -> str:
    """Caption shown under the ticket photo (HTML parse mode)."""
    lines = ["🎫 <b>Your E-Ticket</b>", ""]
    lines.append(f"Code: <b>{html.escape(str(record.get('promo_code', '')))}</b>")
    if record.get("discount") is not None:
        lines.append(f"Discount: <b>{_discount_text(record['discount'])}%</b>")
    if record.get("customer_name"):
        lines.append(f"Issued to: {html.escape(str(record['customer_name']))}")
    lines.append("")
    lines.append("✅ Saved — show this at the counter.")
    return "\n".join(lines)


def send_ticket_to_chat(chat_id: str, record: dict) -> bool:
    """Render the record's ticket and send it to a chat. Returns success."""
    photo = generate_ticket(
        record.get("promo_code", ""),
        record.get("customer_name") or "",
    ).getvalue()
    send_photo(
        chat_id, photo,
        filename=f"ticket_{_safe_filename(record.get('promo_code'))}.jpg",
        caption=ticket_caption(record),
        parse_mode="HTML",
    )
    return True


def _remember_chat(record: dict, chat_id: str, chat_name: str,
                   username: str = "") -> None:
    """Store the customer's Telegram chat on their record.

    The ticket photo has already been delivered by the time this runs, so a
    storage failure here must never bubble out and make Telegram retry the
    update (which would resend the ticket).
    """
    if DRY_RUN:
        return
    fields = {
        "telegram_chat_id": str(chat_id),
        "telegram_chat_name": chat_name,
        "telegram_sent_at": datetime.now().isoformat(timespec="seconds"),
    }
    if username:
        fields["telegram_username"] = username
    try:
        update_record(record["id"], **fields)
    except record_store.StorageError:
        print("ticket delivered, but delivery metadata could not be saved")


# ==================== UPDATE HANDLER ====================

def handle_update(update: dict) -> str:
    """Handle one Telegram update. Returns a short log line."""
    message = update.get("message")
    if not message:
        return "ignored: no message"

    chat       = message.get("chat") or {}
    chat_id    = chat.get("id")
    if isinstance(chat_id, bool) or not isinstance(chat_id, int):
        return "ignored: no chat id"
    # Tickets are private: never look up or send one into a group/channel,
    # where it would leak to everyone and overwrite the stored customer chat.
    if chat.get("type", "private") != "private":
        send_message(chat_id, "🔒 Please message me privately to receive your ticket.")
        return f"ignored: non-private chat {chat_id}"

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

    # ── /start without a token: static welcome, never rate-limited ──
    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        token = parts[1].strip() if len(parts) > 1 else ""
        if not token:
            send_message(chat_id, WELCOME)
            return f"/start from {chat_id}"

        # ── /start <QR token>: a full record scan, so it shares the throttle ──
        if _too_many_lookups(chat_id):
            send_message(chat_id, RATE_LIMITED)
            return f"rate limited {chat_id}"

        record = find_record_by_token(token)
        if not record:
            send_message(chat_id, "❌ This QR code isn't valid any more. "
                                  "Please ask staff for a new one.")
            return f"/start bad token from {chat_id}"

        send_ticket_to_chat(chat_id, record)
        _remember_chat(record, chat_id, chat_name or username, username)
        return f"sent ticket {record.get('promo_code')} to {chat_id}"

    # ── /help: static reply, never rate-limited ──
    if text.startswith("/help"):
        send_message(chat_id, HELP)
        return f"/help from {chat_id}"

    # ── Throttle every remaining lookup path (promo code / name / phone) ──
    if _too_many_lookups(chat_id):
        send_message(chat_id, RATE_LIMITED)
        return f"rate limited {chat_id}"

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
    hits = find_records_by_contact(text)
    if not hits:
        send_message(chat_id,
                     f"❌ I couldn't find a ticket for <b>{html.escape(text)}</b>.\n\n"
                     "Please check the spelling, or scan the QR code on your voucher.")
        return f"no match for {text!r} from {chat_id}"

    if len(hits) == 1:
        record = hits[0]
        send_ticket_to_chat(chat_id, record)
        _remember_chat(record, chat_id, chat_name or username, username)
        return f"sent ticket {record.get('promo_code')} to {chat_id}"

    # Several tickets — ask for the promo code
    lines = [f"🔎 I found <b>{len(hits)}</b> tickets matching "
             f"<b>{html.escape(text)}</b>.",
             "", "Please send the promo code of the one you want:"]
    for r in hits[:10]:
        lines.append(f"• <code>{html.escape(str(r.get('promo_code', '')))}</code> — "
                     f"{html.escape(str(r.get('customer_name') or 'no name'))} "
                     f"({_discount_text(r.get('discount', 0))}%)")
    send_message(chat_id, "\n".join(lines))
    return f"{len(hits)} matches for {text!r} from {chat_id}"
