# Deploy Runbook

Production setup for Vercel or Railway, local operation, and recovery.
The Railway troubleshooting notes include the October 2026 incident that
paused builds platform-wide.

**No credential values appear in this document.** Every secret below is
described by what it does and how to generate it.

---

## 1. How it deploys

### Vercel production activation

1. Import this repository in Vercel with the repository root as **Root
   Directory**. Use automatic Flask detection of root `app.py` and its `app`
   object; do not configure a Gunicorn start command or a static-site build.
   `vercel.json` sets `app.py` to `maxDuration: 60` and includes the ticket
   template, fonts, and HTML templates.
2. Create an Upstash Redis database with REST access. In Vercel **Settings →
   Environment Variables**, set all seven production variables in section 2
   for **Production**. Store secrets in the dashboard, not in git. Use a
   separate Redis database and bot for previews; previews must not replace
   the production bot's webhook or modify production tickets.
3. Deploy to Production (dashboard **Deploy**, or from the repository root
   with an authenticated Vercel CLI: `vercel --prod`). Environment changes
   require a new deployment; they do not update an already-running build.
4. Under **Settings → Domains**, confirm a production `*.vercel.app` domain
   is assigned to this project and latest production deployment. Add/configure
   a custom domain if desired. A successful build without an assigned domain
   is not an activated public app: assign the domain and promote/redeploy the
   intended build to Production before continuing.
5. Set `PUBLIC_URL` to that stable HTTPS origin, for example
   `https://your-project.vercel.app`, with **no** `/telegram/webhook`, path,
   query, or fragment. If the domain changed, redeploy with the corrected
   value before connecting the webhook.
6. Ensure Vercel Deployment Protection/authentication does not block public
   requests to `/telegram/webhook` on the production domain. Telegram cannot
   log into Vercel or send a protection-bypass header. Do not remove app-level
   admin authentication: the webhook itself requires the secret header
   `X-Telegram-Bot-Api-Secret-Token` (missing/wrong header returns 403 when
   configured). Never put that secret in a URL.
7. Open `/admin/login`, sign in, and click **Connect Webhook** in the Telegram
   panel. This registers `PUBLIC_URL + /telegram/webhook` with Telegram.
   Confirm the webhook points to the production domain, not a preview URL.
   Domain/environment deployment alone does not register the webhook.
8. Have the admins and intended customers start a private bot chat as described
   in section 6. Perform deliberate end-to-end testing only after activation.

Vercel's filesystem is not durable; `DATA_FILE` or `/tmp` is not a production
storage solution. Records, QR tokens, known contacts, and webhook update leases
use Upstash REST. Production requires stable `SECRET_KEY` and `ADMIN_PASSWORD`;
local random credentials are not suitable across serverless instances.

### Railway

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

Set these in the hosting dashboard (Railway: service **Variables**; Vercel:
**Settings → Environment Variables → Production**). For Vercel with Telegram,
all seven variables marked yes/Vercel/bot/webhook below are required.
Keep credentials stable across redeploys and instances. Local development alone
can use generated per-run session/admin credentials; do not rely on them in production.

| Variable | Required | Purpose |
|---|---|---|
| `SECRET_KEY` | yes | Stable random key signing admin session cookies. |
| `ADMIN_PASSWORD` | yes | Stable strong admin panel password. |
| `UPSTASH_REDIS_REST_URL` | Vercel | Upstash HTTPS REST endpoint (not a `redis://` connection string). |
| `UPSTASH_REDIS_REST_TOKEN` | Vercel | Upstash REST token with read/write access; configure together with the URL. |
| `TELEGRAM_BOT_TOKEN` | for the bot | From @BotFather. The bot is disabled without it. |
| `TELEGRAM_WEBHOOK_SECRET` | for the webhook | Random string; Telegram echoes it in a header on every update. |
| `PUBLIC_URL` | for the webhook | The public https URL, e.g. `https://your-app.up.railway.app`. Telegram rejects non-HTTPS webhook URLs. |
| `DATA_FILE` | local/Railway JSON only | Path to the JSON store when Upstash is not configured. Point at a mounted volume on Railway. |

### Optional

| Variable | Purpose |
|---|---|
| `TELEGRAM_BOT_USERNAME` | Skips the `getMe` lookup for the bot's @username. Resolved automatically if omitted. |
| `TELEGRAM_ALLOWED_CHAT_IDS` | Comma-separated chat ids allowed to use the bot. Empty means anyone who finds it can look up a ticket. |
| `TELEGRAM_DRY_RUN` | `1` logs messages instead of sending them; creation feedback reports no delivery. Logs can contain ticket/contact details, so use synthetic test data. Unset for real delivery. |
| `TELEGRAM_BACKUP_USERNAMES` | Comma-separated bot-known private usernames; defaults to `President_Alien,SassdahRAK` when unset. Set explicitly blank to disable backups. |
| `CONTACTS_FILE` | Local/Railway JSON mode only: defaults to `telegram_contacts.json`; mount persistently alongside `DATA_FILE`. Not used for Upstash contacts. |
| `PORT` | Railway sets this itself. Do not change it. Not needed on Vercel. |

### Generating values

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Run that once per value you need. `SECRET_KEY` and `TELEGRAM_WEBHOOK_SECRET`
both want 32+ characters of randomness.

---

## 3. Persistence — do not skip this

### Vercel / Upstash

Upstash stores records and QR tokens durably; it also stores the private
username-to-chat mapping and update deduplication leases. Configuring either
REST variable selects Redis; both must be valid. It does not silently fall
back to JSON if Redis is unavailable. Use independent database backups and
restricted admin JSON exports; Telegram ticket copies are not disaster recovery.

### Migrate existing records (operator action, not part of deployment)

1. Pause ticket creation/deletion on the old deployment. Export **JSON** from
   the authenticated admin panel or securely copy its existing `DATA_FILE`.
   Retain this source safely; it contains personal details, chat IDs, and QR tokens.
2. Install dependencies locally and validate the export without network/storage
   requests or writes:

   ```bash
   python3 -m pip install -r requirements.txt
   python3 scripts/migrate_records.py /absolute/path/to/promo_records.json
   ```

3. Only when ready, set `UPSTASH_REDIS_REST_URL` and
   `UPSTASH_REDIS_REST_TOKEN` in the local process environment using a secure
   mechanism. The migration script does **not** load `.env` automatically.
   Point them at the intended empty production database, then explicitly apply:

   ```bash
   python3 scripts/migrate_records.py /absolute/path/to/promo_records.json --apply
   ```

   This preserves record fields/IDs and existing QR tokens, never modifies the
   source, and atomically refuses a nonempty record destination. It is not a
   merge or overwrite command; do not delete production data to bypass refusal.
   The dry run validates JSON/unique nonempty string IDs, not Redis connectivity
   or destination emptiness. This imports records only, not a separate local
   contacts file or old update leases; users/admins must start the bot again
   against the new deployment to populate known contacts.
4. Activate the new production domain/webhook and confirm the imported records
   and existing QR links before reopening creation. Keep the same bot identity
   for existing Telegram QR deep links. Keep the old service read-only/offline
   so two deployments cannot diverge.

### Railway JSON mode

Railway's container filesystem is **ephemeral**. Without a volume, every
deploy and restart wipes `promo_records.json` — which holds the promo codes,
the QR tokens, and the Telegram chat ids of every customer who has received
a ticket. Old QR codes would stop working.

**Setup:**

1. Service → **Volumes** → add a volume, mount path `/data`
2. Set `DATA_FILE=/data/promo_records.json`
3. Set `CONTACTS_FILE=/data/telegram_contacts.json` for known private contacts

The app creates the parent directory if it does not exist. Alternatively use
Upstash on Railway by configuring both REST variables; JSON paths then do not
select the active store. Local JSON mode is for a single process, not a shared
multi-instance production database.

---

## 4. Railway first deploy / local checks

```bash
# 1. Confirm local checks pass
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests -v
python3 validate.py --offline
git diff --check
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

## 6. Telegram onboarding, delivery, and testing

### Known contacts and optional customer auto-send

Bots cannot initiate a private chat from an arbitrary `@username`. Each
recipient must open `https://t.me/<your-bot-username>` and press **Start** in a
**private** conversation first, while the production webhook is active.
They must have a Telegram username; group messages do not establish private
contacts. If an allowlist is enabled, include the intended recipients' chat IDs.

The app remembers usernames case-insensitively without the leading `@` and
resolves creation-time delivery only through this bot-observed mapping. An
optional customer username on a new ticket triggers a send only if known;
otherwise the ticket is saved and the UI says it was not delivered and offers
a bot link when available. It does not discover arbitrary private chats via
Telegram. Successful customer delivery records the customer's chat; failures
or dry runs must not be treated as sent. Starting the bot later does not queue
or automatically retry an earlier creation; use its ticket QR/code to retrieve
it, or a deliberate admin send once the customer chat is associated.

### Admin ticket copies (best-effort backups)

Before creating tickets, have **both `@President_Alien` and `@SassdahRAK`**
start the same production bot privately. These are the defaults when
`TELEGRAM_BACKUP_USERNAMES` is unset. Override with a comma-separated list of
bot-known usernames, or set `TELEGRAM_BACKUP_USERNAMES` explicitly **blank**
to disable. Redeploy after changing production variables. Admin web login
alone does not onboard a Telegram contact or grant a bot username web access.

Each new ticket attempts a separate image-plus-caption copy to each configured
admin: promo code, customer name, phone, discount, and creation time. These are
ticket details only, **not revenue/payment tracking**. Backup delivery does not
reassign the customer's chat or mark the customer's ticket sent. Review each
recipient's `sent`, `unavailable`, or `failed` feedback; missing contacts and
dry runs are not successful delivery.

Copies are **best effort, not guaranteed durable backups**: no durable queue,
background worker, automatic replay, or disaster-recovery restore. Blocked bots,
network/Telegram/storage errors, and request time budgets can prevent copies.
Creation routes establish a shared absolute monotonic deadline **50 seconds**
from before persistence, leaving headroom under Vercel's 60-second function
limit. Customer auto-send starts only with enough remaining time reserved for
contact lookup (13 seconds), upload (11 seconds), and delivery metadata storage
(13 seconds). Persistence and other work consume the same request budget.

`send_ticket_backups(record, *, deadline=None)` uses the earlier of its own
**48-second** deadline and the supplied shared request deadline. Before each
recipient it reserves **24 seconds** (13 for contact lookup plus 11 for upload);
this is a per-recipient admission reserve, **not** a 24-second total backup cap.
Both default admins can receive copies on a fast path, but slow earlier stages
can cause later customer/admin sends to be skipped honestly for lack of time.
There are no guaranteed sends or queued retries.

These deadline checks decide whether to begin another stage; they do not cancel
in-flight work. Requests timeouts bound connection/read inactivity, not strict
wall-clock duration, so a slowly streaming response or rendering work can still
exceed the planned budget and the platform can terminate the function. Photo
uploads use one attempt rather than long retry loops. The ticket remains saved
if subsequent delivery fails. Keep JSON exports and database backups separately;
Telegram images cannot restore records, QR tokens, contacts, or delivery state.
Only configure trusted recipients because copies include personal details.

### From the admin panel

1. **Telegram Bot** panel — the badge should read **connected**
2. **QR** button on any row → the Telegram deep-link QR that opens the bot
   for that ticket. This is distinct from the ticket image's promotion-plans
   QR, which retains its existing plans URL and promo query payload.
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

### Configuration or storage failures

Managed-platform initialization requires `SECRET_KEY`, `ADMIN_PASSWORD`, and
valid storage configuration (Upstash is mandatory on Vercel). Missing/invalid
configuration fails closed; do not interpret a build finishing as proof the
runtime is healthy. Storage failures during requests return 503 rather than
silently using an empty local store. A webhook without a bot token or webhook
secret returns 503. Check dashboard variable scope and redeploy after fixes.

### Photo uploads fail with a gateway timeout

A bad network hop can stall an upload. Photo sends use one bounded attempt,
not a long retry loop. Check the creation page's customer and per-admin delivery
feedback and sanitized service logs. A timeout may mean delivery could not be
confirmed; verify the recipient chat before deliberately retrying to avoid
copies. A saved record is not proof of Telegram delivery.

---

## 8. Routine operations

### Add a new ticket

Admin panel → **Create New Promo Code Ticket** → the preview renders
immediately, and **Download Ticket JPG** saves it. A successfully saved ticket
appears in the records table; persistence depends on the configured durable
store. Customer delivery and admin copies have separate success/failure feedback.

### Export the data

- **Export CSV** — for spreadsheets
- **Export JSON** — the full record store, including chat ids and QR tokens

### Lock the bot down

Set `TELEGRAM_ALLOWED_CHAT_IDS` to the comma-separated chat ids that may
look up tickets. Anyone else gets a refusal.

---

## 9. Security checklist

Run unit tests, `python3 validate.py --offline`, and `git diff --check` before
every deploy. Offline validation does not send Telegram requests or access the
record store. Full `python3 validate.py` is an explicit online integration check
against a running app and configured services, not an offline preflight.
The validation suite's secret checks fail if:

- a Telegram bot token appears in any tracked file
- the admin password appears in any tracked file
- `.env` is tracked by git

The repository is **public**, so anything committed is readable by anyone.
Keep every credential in the Vercel/Railway dashboard and in a local `.env` that is
gitignored.

If a secret was ever pasted into a chat, a log, or a public issue, rotate it
— the old value is compromised regardless of what the code does with it.
