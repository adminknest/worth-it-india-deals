<p align="center"><img src="assets/logo.png" alt="Worth It India" width="220"></p>

# Worth It India deal bot

Worth It India is a general deals platform. The engine accepts every useful category; merchant and destination rules decide whether an individual campaign may be published.

This is the lean pilot engine: it scans authorized deal feeds every minute, rejects stale or weak listings, ranks the rest, removes duplicates, and publishes only a fresh deal that has both an EarnKaro Profit Link and admin approval.

The operating contract is in `AGENT-SPEC.md`; recovery procedures are in `OPERATIONS.md`.

## What it deliberately does

- Reads CSV or JSON from an authorized merchant feed, API export, or manually maintained source.
- Scores current discount, price band, ratings and expected commission.
- Rejects sold-out, stale, invalid-price and low-evidence listings.
- Deduplicates by merchant and product ID.
- Requires a valid EarnKaro link and an explicit `approve` command before Telegram posting.
- Binds approval to the exact deal snapshot and resets it after a price, URL, title or affiliate-link change.
- Requires fresh merchant-page evidence matching the approved price and stock state.
- Enforces a posting interval and daily cap.
- Adds a price-change warning and affiliate disclosure to every post.
- Uses worker leases, a transactional publication reservation, an incident ledger and a hash-chained audit trail.

It does not scrape EarnKaro, Amazon, Flipkart or other stores. EarnKaro's published terms require price verification and prohibit unauthorized reproduction of its platform content. Use official feeds/APIs where available.

## Setup

1. Install Python 3.11 or newer. No third-party Python packages are required.
2. Copy `config.example.json` to `config.json`.
3. Add an authorized CSV/JSON source. `candidates.example.csv` shows the required columns.
4. When Telegram posting is ready, set `TG_BOT_TOKEN` and `TG_CHANNEL_ID` as environment variables. Never paste these into the spreadsheet or source code.

## Pilot commands

```powershell
Copy-Item config.example.json config.json
python dealbot.py ingest candidates.example.csv
python dealbot.py shortlist
python dealbot.py next-link
```

`next-link` prints the best product URL that still needs conversion. Verify it on the merchant page, paste it into EarnKaro's **Make Links** page, and record the full result with the `earnkaro_intake.py record` workflow below. A bare `attach-link` is intentionally insufficient for approval because it does not prove current price or stock.

After recording and approving a verified item:

```powershell
python dealbot.py publish-next --dry-run
python dealbot.py doctor
```

Remove `--dry-run` only after the bot is an administrator in the Telegram channel and both Telegram environment variables are set.

### Telegram admin control

Set `TG_ADMIN_USER_ID` to the owner's numeric Telegram user ID, then run `python operator_bot.py --source candidates.csv`. The owner can use `/queue`, `/approve KEY`, `/reject KEY`, `/publish`, `/pause`, `/resume`, `/doctor`, `/growth` and `/status` in the private bot chat. Commands from every other Telegram account are ignored. The worker records subscriber snapshots every six hours and privately warns the owner when the verified queue remains empty.

A network timeout during publication pauses the operator and marks the send `uncertain`, preventing an automatic retry from creating a duplicate post.

If Telegram's result is ambiguous, inspect the channel and resolve the recorded attempt explicitly with `dealbot.py reconcile`. Never restart the publisher and hope that a retry is safe.

The continuous worker scans every minute while respecting the slower publishing limits in `config.json`:

```powershell
python dealbot.py run candidates.csv
```

## Rate strategy

The engine may inspect thousands of candidates per hour. Publishing at the same rate would be spam and would destroy click-through rate. The pilot defaults to one post every 30 minutes and 24 posts per UTC day. After seven days, adjust from measured views, clicks, orders and unsubscribes.

## EarnKaro pilot (primary now)

EarnKaro is the first pilot adapter. Its logged-in **Make Links** page converts one product URL at a time. The current account requires a Telegram traffic source before it will generate Amazon links. Register the owned channel first; do not add somebody else's channel.

The compliant workflow is:

1. Rank candidates from an authorized merchant feed/export or a current EarnKaro curated page.
2. Open the merchant product page and verify the exact item, price and stock.
3. Generate/copy the Profit Link through EarnKaro's supported UI.
4. Record the verified evidence. The item remains pending until the owner approves it.

```powershell
python earnkaro_intake.py record `
  --merchant Amazon `
  --product-id B0EXAMPLE `
  --title "Exact product title" `
  --merchant-page-url "https://www.amazon.in/dp/B0EXAMPLE" `
  --earnkaro-source-url "https://earnkaro.com/example/PPS1-Amazon" `
  --affiliate-url "https://bitli.in/example" `
  --price 699 --mrp 3499 --category Health `
  --rating-count 500 --commission-pct 5.5 --stock-verified `
  --verification-note "Price and stock visible on merchant page" `
  --terms-snapshot "Tracking within 48h of shipping; confirmation 75 days"
python dealbot.py approve amazon:B0EXAMPLE
python dealbot.py publish-next --dry-run
```

The adapter also imports an EarnKaro report/export without double counting repeated rows and summarizes pending/confirmed commissions:

```powershell
python earnkaro_intake.py import-report commission-report.csv
python earnkaro_intake.py dashboard
python earnkaro_intake.py readiness
```

There is no undocumented endpoint or scraping in this path. Browser-assisted link conversion stays as the narrow manual step until EarnKaro supplies an approved automation method for the account.

## Cuelinks V3 automation

`cuelinks_client.py` is the secondary automated adapter. It is blocked until Cuelinks approves the new account. It can then sync current India campaigns, media rules, offers, links and transactions when the account exposes an API key.

Set the API key in the environment, never in a file:

```powershell
$env:CUELINKS_API_KEY = "..."
python cuelinks_client.py ping
python cuelinks_client.py sync-campaigns
python cuelinks_client.py sync-offers --channel-id YOUR_CHANNEL_ID
python cuelinks_client.py dashboard
```

After reading the current merchant campaign page and confirming a destination:

```powershell
python cuelinks_client.py policy-review CAMPAIGN_ID telegram allow "Reviewed current campaign terms"
python cuelinks_client.py policy-check CAMPAIGN_ID telegram
```

The adapter is credential-ready but has not called the authenticated API because no Cuelinks API key has been supplied.

## Verification

Run:

```powershell
python -m unittest -v test_dealbot.py test_earnkaro_intake.py test_operator_bot.py test_cuelinks_client.py
```

Create a consistent live backup with `python dealbot.py backup data\backups\deals-YYYY-MM-DD.db`. SQLite WAL files are part of live database state; do not copy only the main file while the worker is writing.
