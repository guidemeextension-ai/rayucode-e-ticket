# Deploy Runbook

Everything needed to deploy this app to Railway, and to recover it when
something goes wrong. Written after the October 2026 Railway security
incident, which paused builds platform-wide.

**No credential values appear in this document.** Every secret below is
described by what it does and how to generate it.

---

## 1. How it deploys

| Piece | Detail |
|---|---|
| Repo | `guidemeextension-ai/rayucode-e-ticket` (public) |
| Branch | `main` — a push triggers a build |
| Builder | NIXPACKS (see `railway.json`) |
| Start command | `gunicorn app:app --bind 0.0.0.0:$PORT` |
| Health check | `GET /` must return 200 |
| Runtime | Python 3.11.11 (`runtime.txt`) |

The commit author does not matter — any account with push access triggers the
build. Railway's GitHub App must be installed on the **repo owner**
(`guidemeextension-ai`), which is what the project's Source points at.

---

## 2. Required environment variables

Set these in the Railway dashboard under the service's **Variables** tab.
The app has **no built-in defaults** for the first four — a missing one is
either a random per-run value or a visible startup warning.

| Variable | Required | Purpose |
|---|---|---|
| `SECRET_KEY` | yes | Signs the admin session cookie. Random per run if unset, which logs you out on every restart. |
| `ADMIN_PASSWORD` | yes | Admin panel password. Random per run if unset, printed to the log. |
| `TELEGRAM_BOT_TOKEN` | for the bot | From @BotFather. The bot is disabled without it. |
| `TELEGRAM_WEBHOOK_SECRET` | for the webhook | Random string; Telegram echoes it in a header on every update. |
| `PUBLIC_URL` | for the webhook | The public https URL, e.g. `https://your-app.up.railway.app`. Telegram rejects non-HTTPS webhook URLs. |
| `DATA_FILE` | recommended | Path to the JSON store. Point at a mounted volume. |

### Optional

| Variable | Purpose |
|---|---|
| `TELEGRAM_BOT_USERNAME` | Skips the `getMe` lookup for the bot's @username. Resolved automatically if omitted. |
| `TELEGRAM_ALLOWED_CHAT_IDS` | Comma-separated chat ids allowed to use the bot. Empty means anyone who finds it can look up a ticket. |
| `TELEGRAM_DRY_RUN` | `1` logs messages instead of sending them. Useful for testing. |
| `PORT` | Railway sets this itself. Do not change it. |

### Generating values

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Run that once per value you need. `SECRET_KEY` and `TELEGRAM_WEBHOOK_SECRET`
both want 32+ characters of randomness.

---

## 3. Persistence — do not skip this

Railway's container filesystem is **ephemeral**. Without a volume, every
deploy and restart wipes `promo_records.json` — which holds the promo codes,
the QR tokens, and the Telegram chat ids of every customer who has received
a ticket. Old QR codes would stop working.

**Setup:**

1. Service → **Volumes** → add a volume, mount path `/data`
2. Set `DATA_FILE=/data/promo_records.json`

The app creates the parent directory if it does not exist.

---

## 4. First deploy

```bash
# 1. Confirm local checks pass
python3 -m pip install -r requirements.txt
python3 app.py          # in one terminal
python3 validate.py     # in another — expect 50 passed, 0 failed
```

2. Push to `main`:

```bash
git push origin main
```

3. In Railway, confirm the service **Settings → Source** shows
   `guidemeextension-ai/rayucode-e-ticket`, branch `main`

4. Set the variables from section 2

5. Deploy → check the build log → confirm the health check passes

6. Open the admin panel and click **Connect Webhook** in the Telegram panel.
   This registers the webhook with Telegram using `PUBLIC_URL` and the
   webhook secret. It fails with a clear message if `PUBLIC_URL` is missing
   or not https.

7. Test it — see section 6

---

## 5. Rotating credentials

Railway's October 2026 security notice instructed every user to revoke their
stored environment variables. Do the same for anything that was exposed in a
chat, a log, or a screenshot.

| Variable | How to rotate |
|---|---|
| `TELEGRAM_BOT_TOKEN` | @BotFather → `/revoke` → paste the old token → get a new one. Update the Railway variable. |
| `SECRET_KEY` | Generate a new value. Everyone is logged out; harmless. |
| `ADMIN_PASSWORD` | Generate a new value. |
| `TELEGRAM_WEBHOOK_SECRET` | Generate a new value, then re-run **Connect Webhook** so Telegram learns the new one. |

**After changing the webhook secret, the old one stops working immediately** —
Telegram sends the header you registered last. Re-connect after rotating.

Rotating the bot token does **not** invalidate an existing webhook, but it
does stop the bot from answering until the new token is in the environment.

---

## 6. Testing the Telegram bot

### From the admin panel

1. **Telegram Bot** panel — the badge should read **connected**
2. **QR** button on any row → a QR that opens the bot and delivers that
   ticket
3. **Send** button → re-delivers a ticket to a chat id that has already
   contacted the bot

### End to end

Message the bot from Telegram and send one of:

| Send | Expected |
|---|---|
| Their phone number | That ticket, as a photo |
| Their `@username` | That ticket, as a photo |
| Their name | That ticket, as a photo |
| A promo code | That ticket, as a photo |
| A scanned QR code | The ticket that QR belongs to |
| Something unrecognised | "couldn't find a ticket" |

After a successful send, the record's **Telegram** column flips to
**✓ sent** and the customer's chat id is stored on the record.

### From the command line

```bash
python3 telegram_polling.py
```

Long-polls for messages locally. Requires the webhook to be off — the script
removes it first, and **Disconnect** in the admin panel does the same. Webhook
and polling are mutually exclusive on Telegram.

```bash
python3 telegram_polling.py --qr output/qr
```

Writes one printable QR PNG per record.

---

## 7. Troubleshooting

### Deploys do not trigger

Check in this order:

1. **Railway's status page** — builds were paused platform-wide during the
   October 2026 incident. If builds are still paused, pushes are **not
   queued**; you must re-trigger the deploy manually once they resume.
2. **Settings → Source** in Railway — the repo and branch must be correct.
3. **The GitHub App** — Railway's app must be installed on the repo owner
   `guidemeextension-ai`, not on a personal account. The commit author is
   irrelevant; anyone with push access triggers the build.
4. **Push permissions** — check with:

   ```bash
   gh api repos/guidemeextension-ai/rayucode-e-ticket/collaborators \
     --jq '.[] | {login, push: .permissions.push, admin: .permissions.admin}'
   ```

### The live site is running an old version

A push that happened while builds were paused leaves the deployment on
whatever it was serving before. The site stays up and looks healthy, so this
is easy to miss. **Check the version, not just the status code:**

```bash
# Does the live site have the admin login?
curl -s -o /dev/null -w "%{http_code}\n" https://<your-app>.up.railway.app/admin/login
```

`404` means the deployment predates the auth work. Until Railway rebuilds,
the old build may expose `/admin`, `/admin/export`, `/admin/create` and
`/admin/delete` **without any password**.

If that is the case, treat it as an incident:

1. Check the live build's commit in Railway → the service's deploy history.
2. If the old build has no auth, the admin routes are open to the internet.
   Anyone can read every record, create records, and delete them.
3. Remove the public domain (**Settings → Networking → Remove Domain**) to
   take the site offline until the fixed build is deployed.
4. Re-add it once the new build is live and `/admin/login` returns 200.

### "Address already in use"

Cannot happen in production — Railway assigns the port. Locally the app moves
to the next free port and prints the URL it chose. On Railway the port is
fixed, so the app stays put and fails loudly instead.

### "An HTTPS URL must be provided for webhook"

`PUBLIC_URL` is missing or not https. Set it to the app's public URL.

### "Set TELEGRAM_WEBHOOK_SECRET in your environment first."

Set it in Railway, then click **Connect Webhook** again.

### The bot answers with "This QR code isn't valid any more"

The record store was wiped — the volume is not mounted, or `DATA_FILE` points
somewhere that does not persist. See section 3. Any QR code issued before the
wipe is dead; create new ones afterwards.

### The bot does not answer at all

```bash
# Is a webhook registered?
python3 -c "import telegram_bot as tb; print(tb.get_webhook_info())"
```

- `url` is empty → click **Connect Webhook**
- `last_error_message` is set → the URL is wrong or not https
- A `409 Conflict` in the logs means polling and the webhook are both
  running — disconnect the webhook or stop the polling script

### Photo uploads fail with a gateway timeout

The ticket is ~390 KB and a bad network hop can stall an upload. Uploads
retry up to 3 times with a 120 s budget. If it still fails, check the
service logs and the bot's own error output.

---

## 8. Routine operations

### Add a new ticket

Admin panel → **Create New Promo Code Ticket** → the preview renders
immediately, and **Download Ticket JPG** saves it. Every ticket is logged
permanently and appears in the records table.

### Export the data

- **Export CSV** — for spreadsheets
- **Export JSON** — the full record store, including chat ids and QR tokens

### Lock the bot down

Set `TELEGRAM_ALLOWED_CHAT_IDS` to the comma-separated chat ids that may
look up tickets. Anyone else gets a refusal.

---

## 9. Security checklist

Run `python3 validate.py` before every deploy. It fails if:

- a Telegram bot token appears in any tracked file
- the admin password appears in any tracked file
- `.env` is tracked by git

The repository is **public**, so anything committed is readable by anyone.
Keep every credential in the Railway dashboard and in a local `.env` that is
gitignored.

If a secret was ever pasted into a chat, a log, or a public issue, rotate it
— the old value is compromised regardless of what the code does with it.
