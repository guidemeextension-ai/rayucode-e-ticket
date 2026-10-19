"""
Mini Ticket Generator Web App
=============================
Run locally: python3 app.py → http://localhost:8080
Deployed: https://your-app.railway.app
"""

import io
import os
import base64
from pathlib import Path
from flask import Flask, render_template, request, send_file
from PIL import Image, ImageDraw, ImageFont

app = Flask(__name__)

# -------------------- SETTINGS --------------------
TEMPLATE_PATH = Path("ticket_template.jpg")

X = 2632
Y = 933
FONT_SIZE = 40
TEXT_COLOR = "#FFFFFF"
FONT_PATH = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
CENTER_TEXT = True
# --------------------------------------------------


def find_font(font_size):
    """Load a font or fall back to default."""
    candidates = []
    if FONT_PATH:
        candidates.append(Path(FONT_PATH))

    candidates.extend([
        # macOS (Bold first)
        Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
        Path("/System/Library/Fonts/Supplemental/Helvetica Bold.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/System/Library/Fonts/Supplemental/Helvetica.ttf"),
        Path("/Library/Fonts/Arial Bold.ttf"),
        Path("/Library/Fonts/Arial.ttf"),
        # Linux (Railway/Render servers)
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf"),
        # Windows
        Path("C:/Windows/Fonts/arialbd.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
    ])

    for candidate in candidates:
        try:
            if candidate.is_file():
                return ImageFont.truetype(str(candidate), font_size)
        except OSError:
            pass

    return ImageFont.load_default()


def generate_ticket(promo_code):
    """Generate a ticket image with the given promo code, return as bytes."""
    with Image.open(TEMPLATE_PATH) as source:
        template = source.convert("RGB")

    font = find_font(FONT_SIZE)
    ticket = template.copy()
    draw = ImageDraw.Draw(ticket)

    text_x = X
    if CENTER_TEXT:
        left, top, right, bottom = draw.textbbox((0, 0), promo_code, font=font)
        text_width = right - left
        text_height = bottom - top
        text_x = X - text_width // 2

    # Draw dark pill behind white text for visibility
    if CENTER_TEXT:
        left, top, right, bottom = draw.textbbox((0, 0), promo_code, font=font)
        text_width = right - left
        text_height = bottom - top
        padding_x = 10
        padding_y = 6
        overlay = Image.new("RGBA", ticket.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        overlay_draw.rounded_rectangle(
            [text_x - padding_x, Y - padding_y,
             text_x + text_width + padding_x, Y + text_height + padding_y],
            radius=4,
            fill=(0, 0, 0, 90)
        )
        ticket = Image.alpha_composite(ticket.convert("RGBA"), overlay).convert("RGB")
        draw = ImageDraw.Draw(ticket)

    draw.text((text_x, Y), promo_code, font=font, fill=TEXT_COLOR)

    img_io = io.BytesIO()
    ticket.save(img_io, "JPEG", quality=95, optimize=True)
    img_io.seek(0)
    return img_io


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/generate", methods=["POST"])
def generate():
    code = request.form.get("promo_code", "").strip().upper()
    if not code:
        return render_template("index.html", error="Please enter a promotion code.")

    try:
        img_io = generate_ticket(code)
        img_base64 = base64.b64encode(img_io.getvalue()).decode("utf-8")
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
        return send_file(
            img_io,
            mimetype="image/jpeg",
            as_attachment=True,
            download_name=f"ticket_{code}.jpg"
        )
    except Exception as e:
        return render_template("index.html", error=f"Error: {e}")


if __name__ == "__main__":
    if not TEMPLATE_PATH.is_file():
        print("ERROR: ticket_template.jpg not found! Put it in this folder.")
    else:
        port = int(os.environ.get("PORT", 3000))
        debug = os.environ.get("FLASK_ENV") == "development"
        app.run(host="0.0.0.0", port=port, debug=debug)
