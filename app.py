"""
E-Ticket Generator Web App
==========================
Run locally: python3 app.py → http://localhost:5050 (or the next free port)
"""

import io
import os
import json
import base64
import errno
import hmac
import secrets
import socket
from datetime import datetime
from flask import Flask, render_template, request, send_file, redirect, url_for, jsonify, Response, session
from werkzeug.middleware.proxy_fix import ProxyFix

from ticket_core import (
    TEMPLATE_PATH,
    load_records, save_records, append_record, update_record,
    ensure_qr_tokens, new_qr_token, build_record,
    generate_ticket,
)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)

# Trust Railway's proxy headers so request.url_root is the public https URL.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# -------------------- SETTINGS --------------------

# A missing secret key invalidates every session on restart, so it is
# generated rather than falling back to a shared constant.
SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_urlsafe(48)
app.secret_key = SECRET_KEY

# The admin password is never given a hardcoded fallback: this repository is
# public, so any default here would be a published password. Local runs get
# a random one printed to the console; production must set the variable.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()
if not ADMIN_PASSWORD:
    ADMIN_PASSWORD = secrets.token_urlsafe(12)
    print("=" * 62)
    print("ADMIN_PASSWORD is not set — using a random one for this run:")
    print(f"    {ADMIN_PASSWORD}")
    print("Set it in the environment to keep the same password across restarts.")
    print("=" * 62)
# --------------------------------------------------


# ==================== AUTH ROUTES ====================

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == ADMIN_PASSWORD:
            session["admin_logged_in"] = True
            return redirect(url_for("admin"))
        return render_template("login.html", error="Incorrect password.")
    return render_template("login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_logged_in", None)
    return redirect(url_for("admin_login"))


def admin_required(f):
    """Decorator: redirect to login if not authenticated."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated


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

        record = build_record(code, customer_name, phone, discount_val)
        append_record(record)
        return render_template("index.html", ticket_image=img_base64,
                               promo_code=code, customer_name=customer_name)
    except Exception as e:
        return render_template("index.html", error=f"Error: {e}")


@app.route("/download", methods=["POST"])
def download():
    code          = request.form.get("promo_code", "").strip().upper()
    customer_name = request.form.get("customer_name", "").strip()
    if not code:
        return render_template("index.html", error="No code provided.")
    try:
        img_io = generate_ticket(code, customer_name)
        return send_file(img_io, mimetype="image/jpeg",
                         as_attachment=True, download_name=f"ticket_{code}.jpg")
    except Exception as e:
        return render_template("index.html", error=f"Error: {e}")


# ==================== ADMIN ROUTES ====================

@app.route("/admin")
@admin_required
def admin():
    """Admin panel — records table."""
    records = ensure_qr_tokens(load_records())
    return render_template("admin.html", records=records)


@app.route("/admin/create", methods=["POST"])
@admin_required
def admin_create():
    """Create a new promo code record and generate the ticket."""
    customer_name = request.form.get("customer_name", "").strip()
    phone         = request.form.get("phone", "").strip()
    discount      = request.form.get("discount", "").strip()
    promo_code    = request.form.get("promo_code", "").strip().upper()

    # Validate required fields
    records = load_records()

    if not discount:
        return render_template("admin.html", error="Discount percentage is required.",
                               total=len(records), records=records)
    if not promo_code:
        return render_template("admin.html", error="Promo code is required.",
                               total=len(records), records=records)

    try:
        discount_val = float(discount)
        if not (0 <= discount_val <= 100):
            raise ValueError
    except ValueError:
        return render_template("admin.html", error="Discount must be a number between 0 and 100.",
                               total=len(records), records=records)

    # Build record
    record = build_record(promo_code, customer_name, phone, discount_val,
                          telegram_username=request.form.get("telegram_username", "").strip())
    append_record(record)

    # Generate preview image
    try:
        img_io     = generate_ticket(promo_code, customer_name)
        img_base64 = base64.b64encode(img_io.getvalue()).decode("utf-8")
    except Exception:
        img_base64 = None

    records = ensure_qr_tokens(load_records())
    return render_template("admin.html",
                           success=f"Ticket created for code: {promo_code}",
                           ticket_image=img_base64,
                           promo_code=promo_code,
                           record_id=record["id"],
                           total=len(records), records=records)


# ==================== REPORT ROUTES ====================

@app.route("/admin/report")
@admin_required
def admin_report():
    """Redirect old report URL to admin."""
    return redirect(url_for("admin"))


@app.route("/admin/delete/<record_id>", methods=["POST"])
@admin_required
def admin_delete(record_id):
    """Delete a single promo record by ID."""
    records = load_records()
    records = [r for r in records if r.get("id") != record_id]
    save_records(records)
    return redirect(url_for("admin_report"))


@app.route("/admin/export")
@admin_required
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
@admin_required
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


@app.route("/admin/ticket/<record_id>")
@admin_required
def admin_ticket_download(record_id):
    """Re-download the ticket image for a given record."""
    records = load_records()
    record  = next((r for r in records if r.get("id") == record_id), None)
    if not record:
        return "Record not found", 404
    try:
        img_io = generate_ticket(record["promo_code"], record.get("customer_name") or "")
        return send_file(img_io, mimetype="image/jpeg", as_attachment=True,
                         download_name=f"ticket_{record['promo_code']}.jpg")
    except Exception as e:
        return f"Error generating ticket: {e}", 500


# ==================== TELEGRAM ROUTES ====================

@app.route("/telegram/webhook", methods=["POST"])
def telegram_webhook():
    """Receive updates pushed by Telegram.

    Telegram authenticates itself with the X-Telegram-Bot-Api-Secret-Token
    header, which is the secret we passed to setWebhook.
    """
    import telegram_bot

    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    if secret:
        sent = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(sent, secret):
            return "Forbidden", 403

    update = request.get_json(silent=True) or {}
    try:
        log = telegram_bot.handle_update(update)
        print(f"[telegram] {log}")
    except Exception as e:                                  # noqa: BLE001
        print(f"[telegram] error: {e}")
    return "", 200


@app.route("/admin/telegram/status")
@admin_required
def admin_telegram_status():
    """Bot identity + webhook state, for the admin panel."""
    import telegram_bot

    status = {
        "configured": bool(telegram_bot.BOT_TOKEN),
        "username":    telegram_bot.BOT_USERNAME,
        "dry_run":     telegram_bot.DRY_RUN,
        "webhook":     None,
        "error":       None,
    }
    if not telegram_bot.BOT_TOKEN:
        status["error"] = "TELEGRAM_BOT_TOKEN is not set."
    else:
        try:
            status["username"] = telegram_bot.bot_username()
            info = telegram_bot.get_webhook_info()
            status["webhook"] = {
                "url":                    info.get("url", ""),
                "pending_update_count":   info.get("pending_update_count", 0),
                "last_error_message":     info.get("last_error_message", ""),
                "has_custom_certificate": info.get("has_custom_certificate", False),
            }
        except Exception as e:                              # noqa: BLE001
            status["error"] = str(e)
    return jsonify(status)


@app.route("/admin/telegram/connect", methods=["POST"])
@admin_required
def admin_telegram_connect():
    """Point Telegram's webhook at this deployment."""
    import telegram_bot

    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    if not secret:
        return jsonify({
            "ok": False,
            "error": "Set TELEGRAM_WEBHOOK_SECRET in your environment first.",
        }), 400

    base = os.environ.get("PUBLIC_URL", "").rstrip("/") or request.url_root.rstrip("/")
    url  = f"{base}/telegram/webhook"

    if not url.startswith("https://"):
        return jsonify({
            "ok": False,
            "error": f"Telegram requires an HTTPS URL, but this deployment is at {base}. "
                     "Set PUBLIC_URL to your public https address (e.g. "
                     "https://your-app.up.railway.app) and try again.",
        }), 400

    try:
        telegram_bot.set_webhook(url, secret)
    except Exception as e:                                  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 400

    return jsonify({"ok": True, "url": url})


@app.route("/admin/telegram/disconnect", methods=["POST"])
@admin_required
def admin_telegram_disconnect():
    """Remove the webhook so local polling can take over."""
    import telegram_bot

    try:
        telegram_bot.delete_webhook()
    except Exception as e:                                  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/admin/qr/<record_id>.png")
@admin_required
def admin_qr(record_id):
    """PNG of the ticket's unique QR code."""
    import telegram_bot

    record = next((r for r in load_records() if r.get("id") == record_id), None)
    if not record:
        return "Record not found", 404
    if not record.get("qr_token"):
        record = update_record(record_id, qr_token=new_qr_token()) or record
    try:
        png = telegram_bot.qr_png(record["qr_token"]).getvalue()
    except Exception as e:                                  # noqa: BLE001
        return f"Error generating QR: {e}", 500
    return Response(png, mimetype="image/png",
                    headers={"Content-Disposition": f"inline; filename=qr_{record_id}.png"})


@app.route("/admin/telegram/bot-qr.png")
@admin_required
def admin_bot_qr():
    """QR code that opens the Telegram bot when scanned."""
    import telegram_bot

    if not telegram_bot.BOT_TOKEN:
        return "Bot not configured", 404
    try:
        png = telegram_bot.bot_qr_png().getvalue()
    except Exception as e:                                  # noqa: BLE001
        return f"Error generating QR: {e}", 500
    return Response(png, mimetype="image/png",
                    headers={"Content-Disposition": "inline; filename=bot-qr.png"})


@app.route("/admin/send/<record_id>", methods=["POST"])
@admin_required
def admin_send_ticket(record_id):
    """Send a ticket to the customer's Telegram chat."""
    import telegram_bot

    record = next((r for r in load_records() if r.get("id") == record_id), None)
    if not record:
        return jsonify({"ok": False, "error": "Record not found."}), 404

    chat_id = request.form.get("chat_id", "").strip() or (record.get("telegram_chat_id") or "")
    if not chat_id:
        return jsonify({
            "ok": False,
            "error": "No Telegram chat id yet. The customer must message the bot "
                     "first, or paste a chat id here.",
        }), 400

    try:
        telegram_bot.send_ticket_to_chat(chat_id, record)
    except Exception as e:                                  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 400

    update_record(record_id,
                  telegram_chat_id=str(chat_id),
                  telegram_sent_at=datetime.now().isoformat(timespec="seconds"))
    return jsonify({"ok": True, "chat_id": str(chat_id)})


# ==================== MAIN ====================

def run_server(preferred: int, debug: bool, allow_shift: bool = True) -> None:
    """Start the dev server, moving to the next port if one is taken.

    `allow_shift=False` pins the app to `preferred` — required on Railway,
    where the platform's proxy routes traffic to exactly that port.
    """
    from werkzeug.serving import make_server

    host     = "0.0.0.0"
    attempts = 20 if allow_shift else 1

    def serve() -> None:
        for offset in range(attempts):
            port = preferred + offset
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
                sock.listen(64)
            except OSError as e:
                sock.close()
                if e.errno not in (errno.EADDRINUSE, errno.EACCES):
                    raise
                if not allow_shift:
                    raise SystemExit(
                        f"ERROR: port {port} is required by the platform but is "
                        "in use. Stop whatever is holding it and try again."
                    )
                print(f"Port {port} is in use — trying {port + 1}…")
                continue

            print(f"\n  ➜  http://localhost:{port}\n")
            server = make_server(host, port, app, threaded=True, fd=sock.fileno())
            if debug:
                from werkzeug.debug import DebuggedApplication
                server.application = DebuggedApplication(app, evalex=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                print("\nShutting down.")
            finally:
                server.server_close()
            return

        raise SystemExit(f"Could not bind a port in {preferred}..{preferred + 19}")

    if debug:
        # The reloader re-executes this script, so the socket has to be
        # bound inside the child — not before the reloader starts.
        from werkzeug._reloader import run_with_reloader
        run_with_reloader(serve)
    else:
        serve()


if __name__ == "__main__":
    if not TEMPLATE_PATH.is_file():
        print("ERROR: ticket_template.jpg not found!")
    else:
        preferred = int(os.environ.get("PORT", 5050))
        debug     = os.environ.get("FLASK_ENV") == "development"

        # On Railway the platform fixes the port and its proxy routes to it,
        # so the app must stay on exactly that one. Everywhere else, fall
        # forward to a free port instead of refusing to start.
        on_railway = bool(os.environ.get("RAILWAY_ENVIRONMENT")
                          or os.environ.get("RAILWAY_PROJECT_ID"))
        if on_railway:
            missing = [v for v in ("SECRET_KEY", "ADMIN_PASSWORD")
                       if not os.environ.get(v, "").strip()]
            if missing:
                print("=" * 62)
                print("WARNING: missing environment variables: "
                      + ", ".join(missing))
                print("Sessions will not survive a restart and the admin panel")
                print("is using a random password. Set them in Railway.")
                print("=" * 62)
        run_server(preferred, debug, allow_shift=not on_railway)
