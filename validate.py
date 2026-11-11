"""
System validation for the E-Ticket app and the Telegram bot.
===========================================================
Run it against a running server:

    python3 app.py                # in one terminal
    python3 validate.py           # in another
    python3 validate.py --offline # local renderer/batch checks only; no store writes

Checks every layer in order: local files, the record store, the ticket
renderer, the Telegram API, the webhook endpoint, and the admin UI.
Exits non-zero if any check fails.
"""

import io
import json
import os
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Read the same password the app uses — no hardcoded fallback, since this
# repository is public.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()

results: list[tuple[str, bool, str]] = []


def discover_base() -> str:
    """Find the running app.

    The server picks the next free port when 5050 is taken, so scan a small
    range instead of assuming 5050.
    """
    explicit = os.environ.get("VALIDATE_BASE_URL")
    if explicit:
        return explicit.rstrip("/")

    preferred = int(os.environ.get("PORT", 5050))
    for port in range(preferred, preferred + 20):
        url = f"http://127.0.0.1:{port}"
        try:
            r = requests.get(url, timeout=2)
            if r.status_code == 200:
                return url
        except requests.RequestException:
            continue
    return f"http://127.0.0.1:{preferred}"


# Discovery makes HTTP requests, so defer it until online validation is selected.
BASE = ""


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    return ok


def section(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


# ─────────────────────────── 1. LOCAL FILES ───────────────────────────
def test_files() -> None:
    section("1. Local files")
    check("app.py exists", Path("app.py").is_file())
    check("ticket_core.py exists", Path("ticket_core.py").is_file())
    check("telegram_bot.py exists", Path("telegram_bot.py").is_file())
    check("ticket_template.jpg exists", Path("ticket_template.jpg").is_file())

    import ticket_core
    check("DATA_FILE is set", bool(ticket_core.DATA_FILE), str(ticket_core.DATA_FILE))
    check("bundled font present", Path("fonts/Arial Bold.ttf").is_file(),
          "fonts/Arial Bold.ttf")


# ─────────────────────── 1b. NO SECRETS COMMITTED ───────────────────────
def test_no_secrets_committed() -> None:
    """This repository is public — no token or password may be committed."""
    section("1b. Committed files carry no secrets")

    import re
    import subprocess

    # Patterns that would mean a live credential is in the repo.
    # No \b word boundary: in "bot8905583754:AAH..." the 't' and '8' are both
    # word characters, so a boundary would never match there.
    token_rx   = re.compile(r"\d{8,10}:[A-Za-z0-9_-]{35}")
    password_rx = re.compile(r"welcom to rayu e-ticket")

    def is_placeholder(match: str) -> bool:
        """Ignore obvious documentation placeholders like 123456789:AAxxxx…"""
        secret = match.split(":", 1)[-1]
        return "xxxx" in secret.lower() or len(set(secret)) <= 2

    tracked = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=False
    ).stdout.split()

    scanner = Path(__file__).name
    token_hits, password_hits = [], []

    for name in tracked:
        if not name.endswith((".py", ".html", ".json", ".md", ".txt", ".cfg")):
            continue
        path = Path(name)
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(not is_placeholder(m.group(0)) for m in token_rx.finditer(text)):
            token_hits.append(name)
        # This file holds the detection pattern itself.
        if name != scanner and password_rx.search(text):
            password_hits.append(name)

    check("no Telegram bot token in tracked files", not token_hits, ", ".join(token_hits))
    check("no admin password in tracked files", not password_hits, ", ".join(password_hits))

    # .env must never be tracked
    check(".env is not tracked", ".env" not in tracked)


# ─────────────────────────── 2. TICKET RENDERER ───────────────────────────
def test_renderer() -> None:
    section("2. Ticket renderer")
    from ticket_core import generate_ticket

    try:
        buf = generate_ticket("VALIDATE-1", "Validate Tester")
        data = buf.getvalue()
        check("renders a ticket with a name", len(data) > 50_000, f"{len(data)} bytes")
        check("output is a JPEG", data[:2] == b"\xff\xd8", data[:2].hex())

        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            check("opens as an image", im.size == (1920, 1080), f"{im.size[0]}x{im.size[1]}")
    except Exception as e:                                   # noqa: BLE001
        check("renders a ticket with a name", False, str(e))


def test_plans_qr() -> None:
    """Check the encoded input and actual JPEG module grid without a decoder dep."""
    section("2b. Promotion plans QR")
    from unittest.mock import patch
    from urllib.parse import parse_qs, urlsplit

    import qrcode
    from PIL import Image, ImageChops
    import ticket_core as core

    # Include characters that must never become query syntax, plus Unicode.
    cases = [
        ("PROMO-A8K2P9", "https://rayucode.com/plans?promo=PROMO-A8K2P9"),
        ("virak x mylove", "https://rayucode.com/plans?promo=virak%20x%20mylove"),
        ("A+B&/?=#% café", "https://rayucode.com/plans?promo=A%2BB%26%2F%3F%3D%23%25%20caf%C3%A9"),
    ]
    original_add_data = qrcode.QRCode.add_data
    tile_images = []
    for promo, expected in cases:
        payloads = []

        def capture(qr, data, *args, **kwargs):
            payloads.append(data)
            return original_add_data(qr, data, *args, **kwargs)

        with patch.object(qrcode.QRCode, "add_data", capture):
            buf = core.generate_ticket(promo)
        check(f"exact plans QR payload for {promo!r}", payloads == [expected])
        check("plans query round-trips the entire promo",
              parse_qs(urlsplit(expected).query) == {"promo": [promo]})
        check("renderer returns a rewound BytesIO", isinstance(buf, io.BytesIO) and buf.tell() == 0)

        # Reconstruct the expected symbol independently and sample every module
        # center in the final compressed JPEG, including its four-module border.
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                           box_size=1, border=4)
        qr.add_data(expected)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        left, top, right, bottom = core.PLANS_QR_BOX
        width, height = right - left, bottom - top
        scale = min(width, height) // len(matrix)
        ox = left + (width - len(matrix) * scale) // 2
        oy = top + (height - len(matrix) * scale) // 2
        with Image.open(buf) as image:
            gray = image.convert("L")
            matches = all(
                (gray.getpixel((ox + x * scale + scale // 2,
                                oy + y * scale + scale // 2)) < 128) == dark
                for y, row in enumerate(matrix) for x, dark in enumerate(row)
            )
            check("JPEG QR modules and quiet zone match payload", matches)
            tile_images.append(image.crop(core.PLANS_QR_BOX).copy())
            outside = image.convert("RGB")
        with Image.open(core.TEMPLATE_PATH) as source:
            diff = ImageChops.difference(outside, source.convert("RGB"))
        # Ignore edited regions plus JPEG's neighboring 8px blocks. Everything
        # else must remain visually unchanged after the JPEG recompression.
        from PIL import ImageDraw, ImageStat
        mask = ImageDraw.Draw(diff)
        for box in ((1576, 692, 1838, 949), (520, 840, 1400, 960)):
            mask.rectangle(box, fill=(0, 0, 0))
        check("template outside QR/promo stays intact", max(ImageStat.Stat(diff).mean) < 3)

    check("different promotions produce different QR tiles",
          all(ImageChops.difference(tile_images[0], tile).getbbox() is not None
              for tile in tile_images[1:]))

    with Image.open(core.generate_ticket("NAME-CHECK")) as blank, \
            Image.open(core.generate_ticket("NAME-CHECK", "Validate Tester")) as named:
        difference = ImageChops.difference(blank, named)
        check("optional customer name is still rendered",
              difference.crop((850, 560, 1450, 650)).getbbox() is not None)
        check("customer name does not alter the plans QR",
              difference.crop(core.PLANS_QR_BOX).getbbox() is None)


def test_batch() -> None:
    """Exercise the real batch entry point in a disposable output directory."""
    section("2c. Batch renderer consistency")
    import tempfile
    from unittest.mock import call, patch
    import ticket_core as core
    import ticket_generator as batch

    codes = list(batch.PROMO_CODES)
    with tempfile.TemporaryDirectory(prefix="ticket-validation-") as tmp:
        output = Path(tmp) / "output"
        with patch.object(batch, "OUTPUT_DIR", output), \
                patch.object(batch, "generate_ticket", wraps=core.generate_ticket) as render:
            batch.main()
        check("batch renders configured promos through shared renderer",
              render.call_args_list == [call(code) for code in codes])
        check("batch preserves numbered JPG filenames",
              sorted(p.name for p in output.iterdir()) ==
              [f"ticket_{index:03d}.jpg" for index in range(1, len(codes) + 1)])
        for index, code in enumerate(codes, 1):
            check(f"batch ticket {index} matches shared renderer byte-for-byte",
                  (output / f"ticket_{index:03d}.jpg").read_bytes() ==
                  core.generate_ticket(code).getvalue())


def run_offline() -> None:
    """Fail closed if any offline check attempts networking or store access."""
    from contextlib import ExitStack
    from unittest.mock import patch
    import ticket_core as core

    with ExitStack() as guards:
        for target in ("socket.socket.connect", "socket.create_connection",
                       "requests.sessions.Session.request"):
            guards.enter_context(patch(target, side_effect=AssertionError("offline network access")))
        for name in ("load_records", "save_records", "append_record", "update_record",
                     "ensure_qr_tokens"):
            guards.enter_context(patch.object(core, name,
                                             side_effect=AssertionError("offline record-store access")))
        for test in (test_files, test_renderer, test_plans_qr, test_batch):
            try:
                test()
            except Exception as exc:
                check(f"{test.__name__} crashed", False, str(exc))


# ─────────────────────────── 3. RECORD STORE ───────────────────────────
def test_store() -> None:
    section("3. Record store")
    from ticket_core import load_records, ensure_qr_tokens, build_record, append_record

    records = load_records()
    check("store loads", isinstance(records, list), f"{len(records)} records")

    before = len(records)
    test_rec = build_record("VALIDATE-TMP", "Validation Bot", "+855 00 111 222", 11.0)
    append_record(test_rec)
    records = load_records()
    check("writes a record", len(records) == before + 1)

    new = next((r for r in records if r.get("promo_code") == "VALIDATE-TMP"), None)
    check("new record has a qr_token", bool(new and new.get("qr_token")))
    check("new record has telegram fields",
          bool(new) and "telegram_chat_id" in new and "telegram_sent_at" in new)

    from ticket_core import save_records
    save_records([r for r in records if r.get("promo_code") != "VALIDATE-TMP"])
    check("test record removed", len(load_records()) == before)

    with_qr = ensure_qr_tokens(load_records())
    check("every record has a qr_token",
          all(r.get("qr_token") for r in with_qr))


# ─────────────────────────── 4. LOOKUP LOGIC ───────────────────────────
def test_lookup() -> None:
    section("4. Contact lookup")
    import telegram_bot as tb
    from ticket_core import build_record, append_record, save_records, load_records

    probe = build_record("VALIDATE-LOOKUP", "Zaphod Beeblebrox", "+855 77 888 999", 42.0,
                         telegram_username="@zaphodtest")
    append_record(probe)

    cases = [
        ("exact phone",       "+855 77 888 999"),
        ("phone digits only", "85577888999"),
        ("phone with spaces", "855 77 888 999"),
        ("exact name",        "Zaphod Beeblebrox"),
        ("name lowercased",   "zaphod beeblebrox"),
        ("one name word",     "Beeblebrox"),
        ("telegram username", "@zaphodtest"),
        ("username no @",     "zaphodtest"),
        ("promo code",        "VALIDATE-LOOKUP"),
    ]
    for label, value in cases:
        hits = tb.find_records_by_contact(value)
        found = any(h.get("promo_code") == "VALIDATE-LOOKUP" for h in hits)
        check(f"lookup by {label}", found, f"'{value}' -> {len(hits)} hit(s)")

    check("no false match on nonsense", not tb.find_records_by_contact("zzz-definitely-nobody"))

    save_records([r for r in load_records() if r.get("promo_code") != "VALIDATE-LOOKUP"])


# ─────────────────────────── 5. QR CODES ───────────────────────────
def test_qr() -> None:
    section("5. QR codes")
    import telegram_bot as tb
    from ticket_core import load_records

    rec = next((r for r in load_records() if r.get("qr_token")), None)
    if not check("a record with a qr_token exists", rec is not None):
        return

    link = tb.deep_link(rec["qr_token"])
    check("deep link points at the bot", "t.me/" in link, link)

    try:
        png = tb.qr_png(rec["qr_token"]).getvalue()
        check("renders a QR PNG", len(png) > 500, f"{len(png)} bytes")
        check("QR is a PNG", png[:8] == b"\x89PNG\r\n\x1a\n", png[:8].hex())

        from PIL import Image
        with Image.open(io.BytesIO(png)) as im:
            check("QR opens as an image", im.size[0] > 100, f"{im.size[0]}x{im.size[1]}")
    except Exception as e:                                   # noqa: BLE001
        check("renders a QR PNG", False, str(e))


# ─────────────────────────── 6. TELEGRAM API ───────────────────────────
def test_telegram_api() -> None:
    section("6. Telegram Bot API")
    import telegram_bot as tb

    if not tb.BOT_TOKEN:
        check("TELEGRAM_BOT_TOKEN is set", False, "skipped")
        return
    check("TELEGRAM_BOT_TOKEN is set", True)

    try:
        me = tb.get_me()
        check("getMe succeeds", bool(me.get("username")), "@" + str(me.get("username")))
    except Exception as e:                                   # noqa: BLE001
        check("getMe succeeds", False, str(e))
        return

    try:
        info = tb.get_webhook_info()
        url = info.get("url", "")
        check("getWebhookInfo succeeds", True,
              f"url={url or '(none)'} pending={info.get('pending_update_count')}")
    except Exception as e:                                   # noqa: BLE001
        check("getWebhookInfo succeeds", False, str(e))

    check("bot_username resolves", bool(tb.bot_username()), "@" + tb.bot_username())


# ─────────────────────────── 7. WEB APP ───────────────────────────
def test_web() -> None:
    section("7. Web app")
    try:
        r = requests.get(f"{BASE}/", timeout=15)
        check("GET / returns 200", r.status_code == 200, str(r.status_code))
    except Exception as e:                                   # noqa: BLE001
        check("GET / returns 200", False, str(e))
        return

    # admin requires login
    try:
        r = requests.get(f"{BASE}/admin", timeout=15, allow_redirects=False)
        check("GET /admin redirects when logged out",
              r.status_code == 302 and "/admin/login" in r.headers.get("Location", ""),
              f"{r.status_code} -> {r.headers.get('Location', '')}")
    except Exception as e:                                   # noqa: BLE001
        check("GET /admin redirects when logged out", False, str(e))
        return

    # log in
    if not ADMIN_PASSWORD:
        check("login succeeds", False,
              "ADMIN_PASSWORD is not set — set it in .env or the environment")
        return

    session = requests.Session()
    try:
        r = session.post(f"{BASE}/admin/login", data={"password": ADMIN_PASSWORD},
                         timeout=15, allow_redirects=False)
        check("login succeeds", r.status_code == 302, str(r.status_code))
    except Exception as e:                                   # noqa: BLE001
        check("login succeeds", False, str(e))
        return

    for path, label in [
        ("/admin",                 "admin panel"),
        ("/admin/telegram/status", "telegram status endpoint"),
        ("/admin/export",          "JSON export"),
        ("/admin/export/csv",      "CSV export"),
    ]:
        try:
            r = session.get(f"{BASE}{path}", timeout=15)
            check(f"GET {path} ({label})", r.status_code == 200, str(r.status_code))
        except Exception as e:                               # noqa: BLE001
            check(f"GET {path} ({label})", False, str(e))

    # webhook rejects a bad secret
    try:
        r = requests.post(f"{BASE}/telegram/webhook", json={"update_id": 1},
                          headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
                          timeout=15)
        check("webhook rejects a bad secret", r.status_code == 403, str(r.status_code))
    except Exception as e:                                   # noqa: BLE001
        check("webhook rejects a bad secret", False, str(e))

    # QR route for a real record
    from ticket_core import load_records
    rec = next((r for r in load_records() if r.get("id")), None)
    if rec:
        try:
            r = session.get(f"{BASE}/admin/qr/{rec['id']}.png", timeout=20)
            check("admin QR route returns a PNG",
                  r.status_code == 200 and r.headers.get("Content-Type") == "image/png",
                  f"{r.status_code} {r.headers.get('Content-Type')}")
        except Exception as e:                               # noqa: BLE001
            check("admin QR route returns a PNG", False, str(e))


# ─────────────────────────── 8. BOT REPLIES ───────────────────────────
def test_bot_replies() -> None:
    section("8. Bot replies (dry run)")
    import telegram_bot as tb

    was_dry = tb.DRY_RUN
    tb.DRY_RUN = True
    try:
        def upd(text: str, cid: int = 424242) -> dict:
            return {"update_id": 1, "message": {
                "message_id": 1, "from": {"id": cid, "first_name": "Val"},
                "chat": {"id": cid, "first_name": "Val"}, "text": text}}

        scenarios = [
            ("/start (welcome)",              upd("/start")),
            ("/help",                         upd("/help")),
            ("unknown contact",               upd("zzz-definitely-nobody")),
            ("invalid QR token",              upd("/start bogus-token-xyz")),
        ]
        for label, update in scenarios:
            try:
                log = tb.handle_update(update)
                check(f"bot handles {label}", True, log)
            except Exception as e:                           # noqa: BLE001
                check(f"bot handles {label}", False, str(e))
    finally:
        tb.DRY_RUN = was_dry


# ─────────────────────────── SUMMARY ───────────────────────────
def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true",
                        help="check local ticket/QR/batch rendering without network or store access")
    args = parser.parse_args()
    global BASE
    if not args.offline:
        BASE = discover_base()
    results.clear()
    print("=" * 62)
    print("E-Ticket + Telegram system validation")
    print("Mode: offline (no network or record-store access)" if args.offline else f"Target: {BASE}")
    print("=" * 62)

    if args.offline:
        run_offline()
    else:
        for test in (test_files, test_no_secrets_committed, test_renderer,
                     test_plans_qr, test_batch, test_store, test_lookup,
                     test_qr, test_telegram_api, test_web, test_bot_replies):
            try:
                test()
            except Exception as e:                           # noqa: BLE001
                check(f"{test.__name__} crashed", False, str(e))

    passed = sum(1 for _, ok, _ in results if ok)
    failed = len(results) - passed
    print("\n" + "=" * 62)
    print(f"RESULT: {passed} passed, {failed} failed, {len(results)} total")
    if failed:
        print("\nFailures:")
        for name, ok, detail in results:
            if not ok:
                print(f"  - {name}" + (f" ({detail})" if detail else ""))
    print("=" * 62)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
