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
| `validate.py` | End-to-end check of the whole system (50 checks) |
| `DEPLOY.md` | Deploy runbook: Railway setup, credential rotation, troubleshooting |
| `ticket_template.jpg` | Your ticket design (swap in your own) |
| `templates/` | Web app HTML templates |
| `fonts/` | Bundled `Arial Bold.ttf` so rendering works on any platform |
| `output/` | Generated tickets / QR codes (gitignored) |
| `.env.example` | Template for the environment variables below |

---

## Quick start (web app)

```bash
python3 -m pip install -r requirements.txt
cp .env.example .env      # then fill it in — see "Credentials" below
python3 app.py
```

Open **http://localhost:5050** — the public page generates and downloads tickets.

The admin panel is at **http://localhost:5050/admin**.

If port 5050 is already taken, the app moves to the next free one (5051, 5052, …)
and prints the URL it is using. On Railway the port is fixed by the platform,
so the app stays on exactly that one.

### Using the admin panel

1. **Create New Promo Code Ticket** — promo code and discount are required;
   customer name, phone and `@username` are optional.
2. The ticket preview renders immediately; **Download Ticket JPG** saves it.
3. Every record is logged permanently and can be exported as CSV or JSON.
4. The **Telegram Bot** panel shows the bot's connection state and a QR code
   that opens a chat with it. Each row also gets **QR** and **Send** buttons.

---

## Credentials

**No credentials are stored in this repository, and there are no built-in
defaults.** Every secret comes from the environment, so nothing works until
you set it. This matters because the repository is public — a hardcoded
fallback would be a published password.

Copy `.env.example` to `.env` (gitignored) and fill it in:

```bash
SECRET_KEY=<a long random string>
ADMIN_PASSWORD=<a password of your choosing>
TELEGRAM_BOT_TOKEN=<from @BotFather>
TELEGRAM_WEBHOOK_SECRET=<another long random string>
```

Generate random values with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

If `ADMIN_PASSWORD` is missing, the app generates a random one for that run
and prints it to the console, so the admin panel is never open to a guess.
If `SECRET_KEY` is missing, a random one is generated too — which means
sessions do not survive a restart.

### Rotating credentials

the old value should be treated as compromised. For the bot token, use
[@BotFather](https://t.me/BotFather) to issue a new one, then update
`TELEGRAM_BOT_TOKEN`.

`validate.py` fails if a bot token or the admin password is found in any
tracked file, so a secret cannot be committed by accident.

---

## Telegram bot

Customers get their ticket in Telegram by sending their **phone number**,
**@username**, or **promo code** — or by scanning the **QR code** printed on
their voucher.

### Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Put the token and the webhook secret in `.env` (see "Credentials" above).
3. **Local testing** (no public URL needed):

```bash
python3 telegram_polling.py
```

4. **Production** — open the admin panel and click **Connect Webhook**, or call
   the endpoint directly. Telegram pushes updates to `/telegram/webhook`.

> Webhook and polling are mutually exclusive on Telegram. `telegram_polling.py`
> deletes any webhook before it starts, and connecting the webhook stops
> polling from working.

### How a customer gets their ticket

| The customer sends | The bot replies with |
|---|---|
| Their phone number | That ticket, as a photo |
| Their `@username` | That ticket, as a photo |
| Their name | That ticket, as a photo |
| A promo code | That ticket, as a photo |
| A scanned QR code | The ticket the QR belongs to |

Lookup tries the strongest match first: promo code, then phone number, then
`@username`, then customer name. When several records match, the bot lists
them and asks the customer to send the promo code of the one they want.

### QR codes

Each record gets a unique `qr_token`. Its QR encodes a deep link of the form
`https://t.me/<bot>?start=<token>` — scanning it opens the bot, which replies
with that customer's ticket.

- Admin panel → **QR** button shows the code for one record.
- The Telegram panel also shows a bot-wide QR that just opens the chat, for
  customers who do not have a ticket yet.
- `python3 telegram_polling.py --qr output/qr` writes one PNG per record.

### Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `SECRET_KEY` | required in production | Flask session signing |
| `ADMIN_PASSWORD` | required in production | Admin panel password |
| `DATA_FILE` | recommended on Railway | Path to the JSON store (use a mounted volume) |
| `TELEGRAM_BOT_TOKEN` | for the bot | From @BotFather |
| `TELEGRAM_BOT_USERNAME` | optional | Override; resolved via `getMe` if omitted |
| `TELEGRAM_WEBHOOK_SECRET` | for webhook | Random string, verified on every update |
| `PUBLIC_URL` | optional | Public https base URL |
| `PORT` | optional | Preferred port; the app moves to the next free one locally |
| `TELEGRAM_ALLOWED_CHAT_IDS` | optional | Comma-separated allowlist; empty = anyone |
| `TELEGRAM_DRY_RUN` | optional | `1` logs instead of sending |

---

## Validation

With the app running, check the whole system end to end:

```bash
python3 validate.py
```

50 checks covering the local files, a scan for committed secrets, the record
store, the ticket renderer, every contact-lookup variant, QR generation, the
Telegram Bot API, the web routes (including the auth redirect and the webhook
secret check) and the bot's replies. Exits non-zero if anything fails. It
finds the running app on whichever port it started on.

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
| GET | `/admin/telegram/bot-qr.png` | admin | PNG of the bot-wide QR code |
| POST | `/admin/send/<id>` | admin | Send a ticket to a Telegram chat |
| GET | `/admin/telegram/status` | admin | Bot identity + webhook state |
| POST | `/admin/telegram/connect` | admin | Register the webhook with Telegram |
| POST | `/admin/telegram/disconnect` | admin | Remove the webhook (so polling can take over) |
| POST | `/telegram/webhook` | secret header | Telegram pushes updates here |

---

## Deployment (Railway)

`Procfile` and `railway.json` are preconfigured — Railway runs
`gunicorn app:app` and health-checks `/`.

Set these in the dashboard:

- **`SECRET_KEY`**, **`ADMIN_PASSWORD`** — no defaults, see "Credentials".
- **`TELEGRAM_BOT_TOKEN`**, **`TELEGRAM_WEBHOOK_SECRET`** — for the bot.
- **`PUBLIC_URL`** — the public https URL, so the webhook is registered
  correctly.
- **`DATA_FILE=/data/promo_records.json`** with a mounted volume at `/data`, or
  every deploy wipes your records and Telegram chat ids.

Then open the admin panel and click **Connect Webhook**.

See **[DEPLOY.md](DEPLOY.md)** for the full runbook — first deploy, credential
rotation, testing the bot, and troubleshooting.

---

## Position settings

Text placement lives in `ticket_core.py`:

- `X`, `Y`, `FONT_SIZE` — the promo code box (centered on `X`/`Y`).
- `NAME_X`, `NAME_Y`, `NAME_FONT_SIZE` — the `ISSUED TO:` name line.

Coordinates are measured from the top-left of the 1920×1080 template.

