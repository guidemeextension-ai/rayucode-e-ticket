# E Ticket Promotion Code Generator

Generate JPG tickets with promotion codes using your existing ticket design.

## Files

- `ticket_generator.py` — batch generate tickets for a list of codes (script)
- `app.py` — web app: generate & download tickets in your browser
- `ticket_template.jpg` — your ticket design (add your own)
- `output/` — generated tickets from batch script
- `templates/` — web app HTML templates

---

## Option 1: Web App (Recommended)

A beautiful web interface — type a code, see the ticket, download it instantly.

### Setup

```bash
# Install dependencies
python3 -m pip install Pillow flask
```

### Run

```bash
python3 app.py
```

Then open your browser: **http://localhost:8080**

### How to use

1. Enter a promo code in the input box
2. Click **Generate** → your ticket preview appears
3. Click **Download as JPG** → saves the ticket to your computer
4. Repeat for as many codes as you want!

---

## Option 2: Batch Script

Generate many tickets at once from a list of codes.

### Setup

```bash
python3 -m pip install Pillow
```

### Run

```bash
python3 ticket_generator.py
```

Find your tickets in the `output/` folder.

---

## Position Settings

`X` and `Y` are pixel coordinates from the top-left corner of the ticket image (`0,0` = top-left). Adjust these in either `app.py` or `ticket_generator.py` to match your template.

To center text horizontally around `X`, set `CENTER_TEXT = True`.

---

## Important

- Add your ticket design as `ticket_template.jpg` before running
- The included promo codes are examples — replace with your real codes
- The web app runs on port **8080** (port 5000 is used by macOS AirPlay)
