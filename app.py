"""
E-Ticket Generator Web App
==========================
Run locally: python3 app.py → http://localhost:5050
"""

import io
import os
import json
import base64
import uuid
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, request, send_file, redirect, url_for, jsonify, Response
from PIL import Image, ImageDraw, ImageFont

app = Flask(__name__)

# -------------------- SETTINGS --------------------
TEMPLATE_PATH = Path("ticket_template.jpg")

X          = 961    # Horizontal center of the promo code box (1920×1080 template)
Y          = 899    # Vertical center of the promo code box interior (Y 848–950)
FONT_SIZE  = 55     # Sized to fit the promo code box at 1920×1080 resolution
TEXT_COLOR = "#FFFFFF"
FONT_PATH  = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"

# "ISSUED TO:" name field coordinates
NAME_X         = 1150   # Horizontal center of the name area (after the label)
NAME_Y         = 590    # Vertically aligned with "ISSUED TO:" label (Y 580–600)
NAME_FONT_SIZE = 28     # Smaller font for the name field

# JSON data store
DATA_FILE = Path("promo_records.json")
# --------------------------------------------------


# ==================== DATA HELPERS ====================

def load_records() -> list:
    """Load all promo records from the JSON file."""
    if DATA_FILE.is_file():
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
    return []


def save_records(records: list) -> None:
    """Persist the records list to the JSON file."""
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def append_record(record: dict) -> None:
    """Add a new record and save."""
    records = load_records()
    records.append(record)
    save_records(records)


# ==================== IMAGE HELPERS ====================

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


# ==================== PUBLIC ROUTES ====================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/generate", methods=["POST"])
def generate():
    code          = request.form.get("promo_code", "").strip().upper()
    customer_name = request.form.get("customer_name", "").strip()
    phone         = request.form.get("phone", "").strip()
    discount      = request.form.get("discount", "0").strip()

    if not code:
        return render_template("index.html", error="Please enter a promotion code.")
    try:
        img_io     = generate_ticket(code, customer_name)
        img_base64 = base64.b64encode(img_io.getvalue()).decode("utf-8")

        # Save record to JSON store
        try:
            discount_val = float(discount) if discount else 0.0
        except ValueError:
            discount_val = 0.0

        record = {
            "id":            str(uuid.uuid4()),
            "promo_code":    code,
            "customer_name": customer_name or None,
            "phone":         phone or None,
            "discount":      discount_val,
            "created_at":    datetime.now().isoformat(timespec="seconds"),
        }
        append_record(record)
        return render_template("index.html", ticket_image=img_base64, promo_code=code)
    except Exception as e:
        return render_template("index.html", error=f"Error: {e}")


@app.route("/download", methods=["POST"])
def download():
    code = request.form.get("promo_code", "").strip().upper()
    if not code:
        return render_template("index.html", error="No code provided.")
    try:
        img_io = generate_ticket(code)
        return send_file(img_io, mimetype="image/jpeg",
                         as_attachment=True, download_name=f"ticket_{code}.jpg")
    except Exception as e:
        return render_template("index.html", error=f"Error: {e}")


# ==================== ADMIN ROUTES ====================

@app.route("/admin")
def admin():
    """Admin panel — records table."""
    records = load_records()
    return render_template("admin.html", records=records)


@app.route("/admin/create", methods=["POST"])
def admin_create():
    """Create a new promo code record and generate the ticket."""
    customer_name = request.form.get("customer_name", "").strip()
    phone         = request.form.get("phone", "").strip()
    discount      = request.form.get("discount", "").strip()
    promo_code    = request.form.get("promo_code", "").strip().upper()

    # Validate required fields
    if not discount:
        return render_template("admin.html", error="Discount percentage is required.",
                               total=len(load_records()))
    if not promo_code:
        return render_template("admin.html", error="Promo code is required.",
                               total=len(load_records()))

    try:
        discount_val = float(discount)
        if not (0 <= discount_val <= 100):
            raise ValueError
    except ValueError:
        return render_template("admin.html", error="Discount must be a number between 0 and 100.",
                               total=len(load_records()))

    # Build record
    record = {
        "id":            str(uuid.uuid4()),
        "promo_code":    promo_code,
        "customer_name": customer_name or None,
        "phone":         phone or None,
        "discount":      discount_val,
        "created_at":    datetime.now().isoformat(timespec="seconds"),
    }
    append_record(record)

    # Generate preview image
    try:
        img_io     = generate_ticket(promo_code)
        img_base64 = base64.b64encode(img_io.getvalue()).decode("utf-8")
    except Exception:
        img_base64 = None

    return render_template("admin.html",
                           success=f"Ticket created for code: {promo_code}",
                           ticket_image=img_base64,
                           promo_code=promo_code,
                           total=len(load_records()))


# ==================== REPORT ROUTES ====================

@app.route("/admin/report")
def admin_report():
    """Redirect old report URL to admin."""
    return redirect(url_for("admin"))


@app.route("/admin/delete/<record_id>", methods=["POST"])
def admin_delete(record_id):
    """Delete a single promo record by ID."""
    records = load_records()
    records = [r for r in records if r.get("id") != record_id]
    save_records(records)
    return redirect(url_for("admin_report"))


@app.route("/admin/export")
def admin_export():
    """Download all records as a JSON file."""
    records = load_records()
    json_bytes = json.dumps(records, ensure_ascii=False, indent=2).encode("utf-8")
    return Response(
        json_bytes,
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=promo_records.json"}
    )


@app.route("/admin/export/csv")
def admin_export_csv():
    """Download all records as a CSV file."""
    import csv, io as _io
    records = load_records()
    output = _io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["#", "Promo Code", "Customer Name", "Contact", "Discount (%)", "Created At"])
    for i, r in enumerate(records, 1):
        writer.writerow([
            i,
            r.get("promo_code", ""),
            r.get("customer_name") or "",
            r.get("phone") or "",
            r.get("discount", ""),
            r.get("created_at", ""),
        ])
    csv_bytes = output.getvalue().encode("utf-8")
    return Response(
        csv_bytes,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=promo_records.csv"}
    )


def admin_ticket_download(record_id):
    """Re-download the ticket image for a given record."""
    records = load_records()
    record  = next((r for r in records if r.get("id") == record_id), None)
    if not record:
        return "Record not found", 404
    try:
        img_io = generate_ticket(record["promo_code"])
        return send_file(img_io, mimetype="image/jpeg", as_attachment=True,
                         download_name=f"ticket_{record['promo_code']}.jpg")
    except Exception as e:
        return f"Error generating ticket: {e}", 500


# ==================== MAIN ====================

if __name__ == "__main__":
    if not TEMPLATE_PATH.is_file():
        print("ERROR: ticket_template.jpg not found!")
    else:
        port  = int(os.environ.get("PORT", 5050))
        debug = os.environ.get("FLASK_ENV") == "development"
        app.run(host="0.0.0.0", port=port, debug=debug)
