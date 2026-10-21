"""
Shared ticket core for the E-Ticket web app and the Telegram bot.
==============================================================
Holds everything both the Flask app (app.py) and the bot (telegram_bot.py)
need: the JSON record store and the ticket image renderer.

The store path comes from the DATA_FILE env var so it can point at a
persistent volume on Railway (the container filesystem is ephemeral).
"""

import io
import json
import os
import secrets
import uuid
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# -------------------- SETTINGS --------------------

TEMPLATE_PATH = Path(os.environ.get("TEMPLATE_PATH", "ticket_template.jpg"))
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
# --------------------------------------------------


# ==================== DATA HELPERS ====================

def load_records() -> list:
    """Load all promo records from the JSON file."""
    if DATA_FILE.is_file():
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                records = json.load(f)
            if isinstance(records, list):
                return records
        except (json.JSONDecodeError, OSError):
            return []
    return []


def save_records(records: list) -> None:
    """Persist the records list to the JSON file."""
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def append_record(record: dict) -> None:
    """Add a new record and save."""
    records = load_records()
    records.append(record)
    save_records(records)


def update_record(record_id: str, **fields) -> dict | None:
    """Apply field changes to one record and save. Returns the updated record."""
    records = load_records()
    updated = None
    for r in records:
        if r.get("id") == record_id:
            r.update(fields)
            updated = r
    if updated is not None:
        save_records(records)
    return updated


def new_qr_token() -> str:
    """Short, URL-safe token used in the Telegram deep link for one ticket."""
    return secrets.token_urlsafe(12)


def ensure_qr_tokens(records: list) -> list:
    """Give every record a qr_token if it lacks one. Mutates and returns records."""
    changed = False
    for r in records:
        if not r.get("qr_token"):
            r["qr_token"] = new_qr_token()
            changed = True
    if changed:
        save_records(records)
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


def find_font(font_size: int):
    """Load the best available bold font. Bundled font in fonts/ takes priority."""
    candidates = [
        # Bundled with repo — works on every platform (macOS, Linux, Railway)
        Path("fonts/Arial Bold.ttf"),
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
    for c in candidates:
        try:
            if c.is_file():
                return ImageFont.truetype(str(c), font_size)
        except OSError:
            pass
    return ImageFont.load_default()


def generate_ticket(promo_code: str, customer_name: str = "") -> io.BytesIO:
    """Render the promo code (and optional name) onto the ticket template."""
    with Image.open(TEMPLATE_PATH) as src:
        template = src.convert("RGB")

    ticket = template.copy()
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
