# E-Ticket Promotion Code Generator

Generate JPG tickets with promotion codes using your own ticket design, then
deliver them to customers over Telegram.

## Files

| File | Purpose |
|---|---|
| `ticket_core.py` | Shared record store + ticket image renderer (used by the app *and* the bot) |
| `app.py` | Flask web app: public generator, admin panel, Telegram routes |
| `telegram_bot.py` | Telegram Bot API client, ticket lookup, QR deep links |
| `telegram_polling.py` | Local long-poll runner for testing the bot without a public URL |
| `ticket_generator.py` | Batch-generate tickets for a list of codes |
| `ticket_template.jpg` | Your ticket design (swap in your own) |
| `templates/` | Web app HTML templates |
| `fonts/` | Bundled `Arial Bold.ttf` so rendering works on any platform |
| `output/` | Generated tickets / QR codes (gitignored) |

---

## Quick start (web app)

```bash
python3 -m pip install -r requirements.txt
python3 app.py
```

Open **http://localhost:5050** — the public page generates and downloads tickets.

The admin panel is at **http://localhost:5050/admin** (password from
`ADMIN_PASSWORD`, default is in `app.py`).

### Using the admin panel

1. **Create New Promo Code Ticket** — promo code and discount are required;
   customer name, phone and `@username` are optional.
2. The ticket preview renders immediately; **Download Ticket JPG** saves it.
3. Every record is logged permanently and can be exported as CSV or JSON.

---

## Telegram bot

Customers get their ticket in Telegram by sending their **phone number**,
**@username**, or **promo code** — or by scanning the **QR code** printed on
their voucher.

### Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Copy `.env.example` to `.env` and fill it in:

```bash
TELEGRAM_BOT_TOKEN=123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TELEGRAM_WEBHOOK_SECRET=generate-a-long-random-string
# PUBLIC_URL=https://your-app.up.railway.app   # optional, derived from proxy headers
```

3. **Local testing** (no public URL needed):

```bash
python3 telegram_polling.py
```

4. **Production** — open the admin panel and click **Connect Webhook**, or call
   the endpoint directly. Telegram pushes updates to `/telegram/webhook`.

> Webhook and polling are mutually exclusive on Telegram. `telegram_polling.py`
> deletes any webhook before it starts, and connecting the webhook stops
> polling from working.

### QR codes

Each record gets a unique `qr_token`. Its QR encodes a deep link of the form
`https://t.me/<bot>?start=<token>` — scanning it opens the bot, which replies
with that customer's ticket.

- Admin panel → **QR** button shows the code for one record.
- `python3 telegram_polling.py --qr output/qr` writes one PNG per record.

### Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `SECRET_KEY` | recommended | Flask session signing |
| `ADMIN_PASSWORD` | recommended | Admin panel password |
| `DATA_FILE` | recommended on Railway | Path to the JSON store (use a mounted volume) |
| `TELEGRAM_BOT_TOKEN` | for the bot | From @BotFather |
| `TELEGRAM_BOT_USERNAME` | optional | Override; resolved via `getMe` if omitted |
| `TELEGRAM_WEBHOOK_SECRET` | for webhook | Random string, verified on every update |
| `PUBLIC_URL` | optional | Public https base URL |
| `TELEGRAM_ALLOWED_CHAT_IDS` | optional | Comma-separated allowlist; empty = anyone |
| `TELEGRAM_DRY_RUN` | optional | `1` logs instead of sending |

---

## HTTP routes

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/` | — | Public ticket generator |
| POST | `/generate` | — | Render a ticket (logs a record) |
| POST | `/download` | — | Download a ticket JPG |
| GET/POST | `/admin/login` | — | Admin sign-in |
| GET | `/admin/logout` | — | Sign out |
| GET | `/admin` | admin | Records table + Telegram panel |
| POST | `/admin/create` | admin | Create a record and render its ticket |
| POST | `/admin/delete/<id>` | admin | Delete a record |
| GET | `/admin/export` | admin | All records as JSON |
| GET | `/admin/export/csv` | admin | All records as CSV |
| GET | `/admin/ticket/<id>` | admin | Re-download one ticket JPG |
| GET | `/admin/qr/<id>.png` | admin | PNG of a record's QR code |
| POST | `/admin/send/<id>` | admin | Send a ticket to a Telegram chat |
| GET | `/admin/telegram/status` | admin | Bot identity + webhook state |
| POST | `/admin/telegram/connect` | admin | Register the webhook with Telegram |
| POST | `/admin/telegram/disconnect` | admin | Remove the webhook (so polling can take over) |
| POST | `/telegram/webhook` | secret header | Telegram pushes updates here |

---

## Deployment (Railway)

`Procfile` and `railway.json` are preconfigured — Railway runs
`gunicorn app:app` and health-checks `/`.

Two things to set in the dashboard:

- **`DATA_FILE=/data/promo_records.json`** with a mounted volume at `/data`, or
  every deploy wipes your records and Telegram chat ids.
- **`PUBLIC_URL`** set to the public https URL, so the webhook is registered
  correctly.

---

## Position settings

Text placement lives in `ticket_core.py`:

- `X`, `Y`, `FONT_SIZE` — the promo code box (centered on `X`/`Y`).
- `NAME_X`, `NAME_Y`, `NAME_FONT_SIZE` — the `ISSUED TO:` name line.

Coordinates are measured from the top-left of the 1920×1080 template.
