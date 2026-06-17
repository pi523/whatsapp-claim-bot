# Claim Bot — expense claims over WhatsApp

A tiny, self-hosted expense-claim bot for small teams. Employees submit claims
(taxi / food / other + amount + receipt photo) by chatting privately with a
WhatsApp number; finance receives an auto-generated **Excel per employee** every
month (with clickable receipt links).

- **Low cost** — runs on one machine. No Meta Cloud API, no credit card, no extra server.
- **Self-contained** — ships with its own WhatsApp bridge ([whatsapp-web.js](https://github.com/pedroslopez/whatsapp-web.js)), so you only need one phone number and a QR scan.
- **Isolated** — if the claim service crashes, the bridge call simply fails and the message is left alone. It never blocks your other chats.

> ⚠️ The bridge uses **whatsapp-web.js**, an unofficial library that automates
> WhatsApp Web. It is not affiliated with or endorsed by WhatsApp/Meta. Use a
> number you control and review WhatsApp's Terms before deploying.

## Architecture

```
WhatsApp (private chat)
   │
   ▼
bridge/whatsapp_bridge.js   (whatsapp-web.js, scans a QR once)
   ├─ claim messages ──► POST 127.0.0.1:5005 ─► claim_service.py (Flask)
   │                                              ├─ SQLite (claims.db)
   │                                              ├─ receipts/  (originals kept forever)
   │                                              └─ monthly ─► claim_export.py ─► Excel per employee
   └─ monthly report queue ◄──────────────────────────────────────────┘  (bridge sends files to finance)

receipt-worker/   optional Cloudflare Worker that hosts compressed receipt
                  images behind unguessable links (KV, auto-expire after 60 days)
```

## Components

| Path | Role |
|------|------|
| `claim_service.py` | Claim service: conversation state machine + SQLite + receipt storage + monthly scheduler (Flask on `127.0.0.1:5005`) |
| `claim_export.py` | Builds one Excel per employee (3 tabs: Taxi / Food / Other) and queues them to finance over WhatsApp |
| `bridge/` | Standalone WhatsApp bridge (whatsapp-web.js). Forwards private messages to the service and drains the report queue |
| `receipt-worker/` | Optional Cloudflare Worker that hosts receipt images (upload / view, KV with TTL) |
| `claim_config.json.example` | Config template — copy to `claim_config.json` and fill in |
| `start_claim.sh` | Start the claim service under pm2 (process name `claim-service`) |
| `requirements.txt` | Python deps: Flask / openpyxl / Pillow |

## Requirements

- Python 3.9+
- Node.js 18+ (for the bridge; whatsapp-web.js pulls in Chromium via Puppeteer)
- A spare WhatsApp number to act as the bot
- (Optional) A free Cloudflare account for the receipt Worker

## Setup

### 1. Install dependencies
```bash
pip3 install -r requirements.txt
cd bridge && npm install && cd ..
```

### 2. Configure
```bash
cp claim_config.json.example claim_config.json
```
Edit `claim_config.json`. The minimum is `finance_whatsapp` (who receives the
monthly report).

| Field | Meaning |
|-------|---------|
| `finance_whatsapp` | Finance number: bare digits (e.g. `15551234567`) or `xxx@c.us` / group `xxx@g.us`. Leave empty to only generate files, not send |
| `export_day` / `export_hour` | Day-of-month / hour to auto-send last month's report (default 1st, 09:00) |
| `currency` / `currency_symbol` | e.g. `"USD"` / `"$"`, `"EUR"` / `"€"`, `"SGD"` / `"S$"` |
| `proactive_queue_path` | Shared file the bridge drains to send reports (default `/tmp/wa_proactive_queue.json`) |
| `receipt_host_url` / `receipt_upload_secret` / `receipt_ttl_days` | Cloudflare receipt Worker settings (optional — see below) |
| `port` | Claim service port (default `5005`) |

### 3. Start the claim service
```bash
bash start_claim.sh           # uses pm2; or: python3 claim_service.py
curl http://127.0.0.1:5005/health
```

### 4. Start the WhatsApp bridge and link the phone
```bash
cd bridge && npm start
```
Scan the QR code with **WhatsApp → Linked devices** on the bot's phone. The
session is saved locally (`bridge/.wwebjs_auth/`), so you only scan once.

### 5. Test from any phone
Privately message the bot number:
```
claim → enter your name → pick 1/2/3 → enter amount → send receipt photo → (Other) note → ✅
mine  → see your claims this month
```
Verify it was stored:
```bash
sqlite3 claims.db "SELECT employee_name,type,amount,note,receipt_path FROM claims;"
ls receipts/
```

### 6. Monthly report (verify once)
```bash
python3 claim_export.py 2026-06     # specific month; no arg = previous month
```
This writes `exports/<month>/<Name>_<YYYY-MM>.xlsx` per employee and, if
`finance_whatsapp` is set and the bridge is running, queues them to finance.

## Optional: receipt image hosting (receipt-worker)

Originals always stay in local `receipts/`. When a report is built, each receipt
is compressed and uploaded to a Cloudflare Worker; the Excel stores an
unguessable view link that auto-expires (KV TTL, default 60 days). After expiry,
regenerating the report re-uploads from the local original.

```bash
cd receipt-worker
npx wrangler kv namespace create RECEIPTS     # paste the id into wrangler.toml
npx wrangler secret put UPLOAD_SECRET         # must match receipt_upload_secret in claim_config.json
npx wrangler deploy
```
Then set `receipt_host_url` in `claim_config.json` to your deployed
`https://claim-receipts.<your-subdomain>.workers.dev`. If you skip this, reports
still generate — the Receipt column just shows `(receipt on file)` instead of a link.

## Employee commands (private chat, English)

| Action | Send |
|--------|------|
| Start a claim | `claim` / `expense` |
| Pick type | `1` (Taxi) / `2` (Food) / `3` (Other) |
| See this month | `mine` / `history` |
| Cancel current claim | `cancel` |

## Finance commands (private chat)

Finance is recognised automatically by the `finance_whatsapp` number — no setup.

| Action | Send |
|--------|------|
| One employee, this month | `report <name>` |
| One employee, a month | `report <name> 2026-05` |
| Everyone, this month | `report all` |
| List staff + monthly totals | `employees` |

On the 1st of each month the bot also auto-sends every employee's report for the
previous month to `finance_whatsapp`.

## Operations

```bash
pm2 logs claim-service
pm2 restart claim-service
```

The bridge can run under pm2 too:
```bash
pm2 start bridge/whatsapp_bridge.js --name claim-bridge
```

## Notes & limits

- Claims trigger in **private chats only**; group chats are ignored.
- `mine` / `history` only respond for already-registered employees, to avoid
  swallowing unrelated messages.
- Designed for small teams (roughly < 30 people) on local SQLite + a local
  receipts folder. There is no per-claim approval flow — finance reviews the
  monthly Excel.
- `claims.db`, `receipts/`, and `claim_config.json` hold business data/secrets
  and are git-ignored — never commit them.

## License

MIT — see [LICENSE](LICENSE).
