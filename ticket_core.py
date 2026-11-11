"""
Shared ticket core for the E-Ticket web app and the Telegram bot.
==============================================================
Holds everything both the Flask app (app.py) and the bot (telegram_bot.py)
need: the durable record facade and the ticket image renderer.

Upstash is used when configured; local JSON uses the patchable DATA_FILE.
Vercel requires durable storage rather than writing ephemeral local files.
"""

import io
import record_store
import os
import secrets
import threading
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import qrcode
from PIL import Image, ImageDraw, ImageFont

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# -------------------- SETTINGS --------------------

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = Path(os.environ.get("TEMPLATE_PATH", str(BASE_DIR / "ticket_template.jpg")))
DATA_FILE     = Path(os.environ.get("DATA_FILE", "promo_records.json"))

X          = 961    # Horizontal center of the promo code box (1920×1080 template)
Y          = 899    # Vertical center of the promo code box interior (Y 848–950)
FONT_SIZE  = 55     # Sized to fit the promo code box at 1920×1080 resolution
TEXT_COLOR = "#FFFFFF"
FONT_PATH  = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"

# "ISSUED TO:" name field coordinates
NAME_X         = 1150   # Horizontal center of the name area (after the label)
NAME_Y         = 590    # Vertically aligned with "ISSUED TO:" label (Y 580–600)
NAME_FONT_SIZE = 28     # Smaller font for the name field

# Measured baked-in QR tile, inside the decorative frame (right/bottom exclusive).
PLANS_QR_BOX = (1584, 700, 1830, 941)
# --------------------------------------------------

# -------------------- RENDER CACHES --------------------
# The template is decoded once and reused until the file on disk changes. The
# cache holds a single (key, image) entry; callers always receive a copy so the
# cached image can never be mutated. Fonts are immutable and cached per size.
_TEMPLATE_CACHE_LOCK = threading.Lock()
_TEMPLATE_CACHE = None  # (key, RGB image) or None

_FONT_CACHE_LOCK = threading.Lock()
_FONT_CACHE = {}
# ------------------------------------------------------


# ==================== DATA HELPERS ====================

def load_records() -> list:
    """Load durable records, or the explicit local DATA_FILE."""
    return record_store.load_records(DATA_FILE)


def save_records(records: list) -> None:
    """Atomically replace the records list (prefer granular mutations)."""
    record_store.save_records(DATA_FILE, records)


def append_record(record: dict) -> None:
    """Atomically add a new record."""
    record_store.append_record(DATA_FILE, record)


def update_record(record_id: str, **fields) -> dict | None:
    """Apply field changes to one record and save. Returns the updated record."""
    return record_store.update_record(DATA_FILE, record_id, **fields)


def delete_record(record_id: str) -> bool:
    """Atomically delete one record, returning whether it existed."""
    return record_store.delete_record(DATA_FILE, record_id)


def new_qr_token() -> str:
    """Short, URL-safe token used in the Telegram deep link for one ticket."""
    return secrets.token_urlsafe(12)


def ensure_qr_tokens(records: list) -> list:
    """Give every record a qr_token if it lacks one. Mutates and returns records."""
    for r in records:
        if not r.get("qr_token"):
            updated = record_store.update_record(
                DATA_FILE, r["id"], only_missing=True, qr_token=new_qr_token())
            if updated is not None:
                r.update(updated)
    return records


def build_record(promo_code: str, customer_name: str, phone: str,
                 discount: float, telegram_username: str = "") -> dict:
    """Build a new promo record with the fields the bot and admin panel share."""
    return {
        "id":                str(uuid.uuid4()),
        "promo_code":        promo_code,
        "customer_name":     customer_name or None,
        "phone":             phone or None,
        "discount":          discount,
        "telegram_username": telegram_username or None,
        "qr_token":          new_qr_token(),
        "telegram_chat_id":  None,
        "telegram_chat_name": None,
        "telegram_sent_at":  None,
        "created_at":        datetime.now().isoformat(timespec="seconds"),
    }


def _template_cache_key(path: Path) -> tuple:
    """Identity of the template file: resolved path, mtime and size."""
    resolved = path.resolve()
    stat = resolved.stat()
    return (str(resolved), stat.st_mtime_ns, stat.st_size)


def _load_template() -> Image.Image:
    """Return a private RGB copy of the decoded template, decoded at most once.

    The decoded image is cached and keyed by the template's identity so that
    replacing the file invalidates the cache. The copy handed to the caller is
    made after the lock is released, so no caller can mutate the cached image.
    """
    global _TEMPLATE_CACHE
    key = _template_cache_key(TEMPLATE_PATH)
    with _TEMPLATE_CACHE_LOCK:
        cached = _TEMPLATE_CACHE
        if cached is None or cached[0] != key:
            with Image.open(TEMPLATE_PATH) as src:
                cached = (key, src.convert("RGB"))
            _TEMPLATE_CACHE = cached
        template = cached[1]
    return template.copy()


def find_font(font_size: int):
    """Load the best available bold font. Bundled font in fonts/ takes priority."""
    with _FONT_CACHE_LOCK:
        cached = _FONT_CACHE.get(font_size)
    if cached is not None:
        return cached

    candidates = [
        # Bundled with repo — works on every platform (macOS, Linux, Railway)
        BASE_DIR / "fonts/Arial Bold.ttf",
        # macOS system fonts (fallback for local dev if bundled font missing)
        Path(FONT_PATH),
        Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
        Path("/System/Library/Fonts/Supplemental/Helvetica Bold.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/Library/Fonts/Arial Bold.ttf"),
        Path("/Library/Fonts/Arial.ttf"),
        # Linux system fonts (fallback)
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"),
        # Windows
        Path("C:/Windows/Fonts/arialbd.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
    ]
    font = None
    for c in candidates:
        try:
            if c.is_file():
                font = ImageFont.truetype(str(c), font_size)
                break
        except OSError:
            pass
    if font is None:
        font = ImageFont.load_default()

    with _FONT_CACHE_LOCK:
        # Another thread may have populated the cache while we loaded the file.
        return _FONT_CACHE.setdefault(font_size, font)


def generate_ticket(promo_code: str, customer_name: str = "") -> io.BytesIO:
    """Render a JPEG ticket with its promo-specific Rayu plans QR and optional name."""
    ticket = _load_template()

    # Replace the static template QR completely, preserving its outer frame.
    # Integer module sizes avoid blurred edges; border=4 supplies the quiet zone.
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                       box_size=1, border=4)
    qr.add_data("https://rayucode.com/plans?promo=" + quote(promo_code, safe=""))
    qr.make(fit=True)
    left, top, right, bottom = PLANS_QR_BOX
    width, height = right - left, bottom - top
    module_count = qr.modules_count + 2 * qr.border
    qr.box_size = min(width, height) // module_count
    if qr.box_size < 1:
        raise ValueError("Promotion code is too long for the ticket QR tile")
    qr_image = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    tile = Image.new("RGB", (width, height), "white")
    tile.paste(qr_image, ((width - qr_image.width) // 2,
                         (height - qr_image.height) // 2))
    ticket.paste(tile, (left, top))
    draw   = ImageDraw.Draw(ticket)

    # ── Promo code (centered in promo box) ──
    font_code   = find_font(FONT_SIZE)
    bbox        = draw.textbbox((0, 0), promo_code, font=font_code)
    text_width  = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    draw.text(
        (X - text_width // 2, Y - text_height // 2),
        promo_code, font=font_code, fill=TEXT_COLOR
    )

    # ── Customer name on ISSUED TO line (optional) ──
    if customer_name:
        font_name = find_font(NAME_FONT_SIZE)
        nbbox     = draw.textbbox((0, 0), customer_name, font=font_name)
        nw        = nbbox[2] - nbbox[0]
        nh        = nbbox[3] - nbbox[1]
        draw.text(
            (NAME_X - nw // 2, NAME_Y - nh // 2),
            customer_name, font=font_name, fill=TEXT_COLOR
        )

    img_io = io.BytesIO()
    ticket.save(img_io, "JPEG", quality=95, optimize=True)
    img_io.seek(0)
    return img_io
