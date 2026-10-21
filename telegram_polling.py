"""
Local Telegram bot runner (long polling).
=========================================
Use this to test the bot from your Mac without a public HTTPS URL.

    python3 telegram_polling.py

Note: Telegram does not allow getUpdates while a webhook is set. If you
connected the webhook from the admin panel, run this script to switch back
to polling (it deletes the webhook first).

Flags:
    --qr [outdir]   write a QR PNG for every record to outdir (default output/qr)
"""

import sys
import time
from pathlib import Path

import telegram_bot
from ticket_core import load_records, ensure_qr_tokens

POLL_TIMEOUT = 25


def export_qrs(outdir: str) -> None:
    """Write one QR PNG per record so they can be printed on vouchers."""
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    records = ensure_qr_tokens(load_records())
    for r in records:
        code = r.get("promo_code", "unknown")
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in code)
        path = out / f"qr_{safe}_{r.get('id', '')[:8]}.png"
        path.write_bytes(telegram_bot.qr_png(r["qr_token"]).getvalue())
        print(f"Created: {path}  |  {code}")
    print(f"\n{len(records)} QR code(s) written to {out}/")


def main() -> int:
    if "--qr" in sys.argv:
        idx = sys.argv.index("--qr")
        outdir = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else "output/qr"
        export_qrs(outdir)
        return 0

    if not telegram_bot.BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN is not set.")
        print("Export it first, e.g.:  export TELEGRAM_BOT_TOKEN=123456:ABC-DEF")
        return 1

    # Polling and webhooks are mutually exclusive — clear any webhook first.
    try:
        info = telegram_bot.get_webhook_info()
        if info.get("url"):
            print("A webhook is set — removing it so polling can take over.")
            telegram_bot.delete_webhook()
    except telegram_bot.TelegramError as e:
        print(f"ERROR talking to Telegram: {e}")
        return 1

    try:
        me = telegram_bot.get_me()
        print(f"Bot: @{me.get('username')}  ({me.get('first_name')})")
    except telegram_bot.TelegramError as e:
        print(f"ERROR talking to Telegram: {e}")
        return 1

    print("Polling for messages — press Ctrl+C to stop.\n")

    offset = None
    try:
        while True:
            try:
                updates = telegram_bot._api("getUpdates", timeout=POLL_TIMEOUT,
                                            offset=offset if offset is not None else -1)
            except telegram_bot.TelegramError as e:
                print(f"poll error: {e}")
                time.sleep(3)
                continue

            for update in updates:
                offset = update["update_id"] + 1
                try:
                    log = telegram_bot.handle_update(update)
                    print(f"[{datetime_now()}] {log}")
                except Exception as e:                      # noqa: BLE001
                    print(f"[{datetime_now()}] handler error: {e}")

    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


def datetime_now() -> str:
    from datetime import datetime
    return datetime.now().strftime("%H:%M:%S")


if __name__ == "__main__":
    sys.exit(main())
