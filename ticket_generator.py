"""
E Ticket Promotion Code Generator
=================================
Place your JPG ticket design beside this script as:
    ticket_template.jpg

Edit PROMO_CODES and the code placement settings below, then run:
    python3 ticket_generator.py

Install dependency:
    python3 -m pip install Pillow

The script creates one JPG per promotion code in the output/ folder.
"""

from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

# -------------------- SETTINGS --------------------

TEMPLATE_PATH = Path("ticket_template.jpg")
OUTPUT_DIR = Path("output")

PROMO_CODES = [
    "PROMO-A8K2P9",
    "PROMO-B4M7X1",
    "PROMO-C9D3W6",
    "virak x mylove",
]

# Position in pixels, measured from the top-left corner of the image.
# Centered inside the promo code box (template is 5280×2970px).
X = 2635          # Horizontal center of the promo code box
Y = 2480          # Vertical center of the promo code box interior (2365–2595)

FONT_SIZE = 140   # Sized to fit the box at full 5280×2970 resolution
TEXT_COLOR = "#FFFFFF"  # White text to show on the dark box background
# Optional: set to a font file path, or leave None for automatic selection.
FONT_PATH = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"

# Optional: center the code around X instead of starting at X.
CENTER_TEXT = True

# JPEG output quality: 1–95. Higher means better quality and larger files.
JPEG_QUALITY = 95

# --------------------------------------------------


def find_font(font_path, font_size):
    """Load the chosen font or try common system fonts."""
    candidates = []

    if font_path:
        candidates.append(Path(font_path))

    # Common macOS fonts
    candidates.extend([
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/System/Library/Fonts/Supplemental/Helvetica.ttf"),
        Path("/Library/Fonts/Arial.ttf"),
        # Common Linux fonts
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
        # Common Windows fonts
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("C:/Windows/Fonts/Arial.ttf"),
    ])

    for candidate in candidates:
        try:
            if candidate.is_file():
                return ImageFont.truetype(str(candidate), font_size)
        except OSError:
            pass

    print("Warning: no TrueType font found; using Pillow's default font.")
    return ImageFont.load_default()


def main():
    if not TEMPLATE_PATH.is_file():
        print(f"ERROR: Could not find ticket template: {TEMPLATE_PATH}")
        print("Put your JPG ticket design in this folder and name it ticket_template.jpg.")
        return

    if not PROMO_CODES:
        print("ERROR: PROMO_CODES is empty. Add at least one promotion code.")
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    with Image.open(TEMPLATE_PATH) as source:
        template = source.convert("RGB")

    font = find_font(FONT_PATH, FONT_SIZE)

    print(f"Template size: {template.width} x {template.height} pixels")
    print(f"Code position: X={X}, Y={Y}")
    print(f"Generating {len(PROMO_CODES)} ticket(s)...")

    for index, code in enumerate(PROMO_CODES, start=1):
        ticket = template.copy()
        draw = ImageDraw.Draw(ticket)

        # Center the text horizontally around X if requested.
        text_x = X
        if CENTER_TEXT:
            left, top, right, bottom = draw.textbbox((0, 0), code, font=font)
            text_width = right - left
            text_x = X - text_width // 2

        # Draw the promotion code on top of the blank area.
        draw.text((text_x, Y), code, font=font, fill=TEXT_COLOR)

        # Use a safe filename based on the ticket number.
        output_path = OUTPUT_DIR / f"ticket_{index:03d}.jpg"
        ticket.save(output_path, "JPEG", quality=JPEG_QUALITY, optimize=True)

        print(f"Created: {output_path}  |  Code: {code}")

    print("\nFinished! Check the output/ folder for your tickets.")
    print("Tip: Open one generated ticket and adjust X, Y, FONT_SIZE, or TEXT_COLOR if needed.")


if __name__ == "__main__":
    main()
