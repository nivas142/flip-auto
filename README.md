# flip-auto monitor

This project includes a Python program (`monitor.py`) that:

- Checks IMAP email for configured sender addresses.
- Reads email subject/body and matches configured cities.
- Checks a Google Sheet for matching city rows.
- Sends Telegram alerts for new matches (Twilio optional fallback).
- Stores sent IDs in a local state file to prevent duplicate texts.
- Extracts ask, rehab, sqft, beds/baths, year built, and risk flags.
- Calculates a configurable first-pass profit, basis percentage, MAO, and lead score.
- Deduplicates structured deals by normalized address and ask across mailboxes/senders.

## Pre-screening engine

Stage-one underwriting activates only when an independent valuation provider is
configured. It is designed to prioritize leads for an ARMLS CMA, not approve a
purchase. Sender-provided ARV is never extracted, displayed, or used.

Cloud CMA supplies ARMLS report data asynchronously through a private result
email. The engine requests a report once per unique address, extracts only
closed-sale prices from the PDF, applies its own size/year/type/bed/bath and
recency rules, and calculates weighted low/likely/high ARV estimates. Cloud
CMA's average or suggested value is never consumed. The conservative low
estimate is used for profit and MAO calculations.

Configure these sections in private `config.yaml`:

```yaml
state_max_seen: 2000
valuation:
  enabled: true
  provider: cloud_cma
  api_key: YOUR_CLOUD_CMA_API_KEY
  result_email: you@example.com
  result_folder: INBOX
  template: Web Leads
  min_listings: 25
  max_requests_per_run: 3
  max_requests_per_day: 10
  request_ttl_days: 30
  max_radius: 1.0
  days_old: 180
  size_tolerance: 0.20
  year_tolerance: 10
  minimum_comps: 3
screening:
  enabled: true
  target_profit: 50000
  selling_cost_percent: 0.07
  other_costs: 12000
  max_basis_percent: 0.80
  default_rehab_per_sqft: 20
  fallback_rehab: 35000
```

Important safeguards:

- Sender-provided ARV is ignored completely.
- Cloud CMA averages/suggested values and active/pending prices never feed ARV;
  only eligible closed-sale records do.
- Cloud CMA PDFs do not expose numeric comp distance. Until a broker-approved
  structured feed supplies it, results remain low-confidence and require final
  ARMLS radius verification.
- Automated comp scores are capped at 84, below the 85+ immediate tier.
- Requests are capped per run/day and cached by an opaque address hash for 30
  days. Raw MLS report data is not committed to the repository or sent to Telegram.
- Missing ask or independent comp ARV produces `VALUATION REQUIRED` rather than a guessed result.
- Rehab is labeled as provided or assumed. When absent, the engine uses configured
  dollars per sqft, then a flat fallback when sqft is also missing.
- Structured multi-property emails produce one alert per matching property, even
  when the properties are in different configured cities.

Default calculations:

```text
Preliminary profit = conservative independent ARV - ask - rehab - selling costs - other costs
Basis % = (ask + rehab) / conservative independent ARV
Target MAO = conservative independent ARV - rehab - selling costs - other costs - target profit
```

Tune these settings under `screening`:

- `target_profit`
- `selling_cost_percent`
- `other_costs`
- `max_basis_percent`
- `default_rehab_per_sqft`
- `fallback_rehab`

Set `screening.enabled: false` in private config to retain the legacy
email-level alert format.

## Setup

1. Create a virtual environment and install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

2. Configure:

```powershell
Copy-Item config.example.yaml config.yaml
```

Edit `config.yaml`:
- Optional `screening` overrides for stage-one underwriting assumptions and scoring.
- `email.sender_filters` for allowed senders.
- `email.subject_filters` for subject phrases that must also match when set.
- `email.lookback_hours` or `email.lookback_minutes` for how far back IMAP email should be scanned.
- `email.cities` and `gsheet.cities` for target cities.
- The default template includes a Flip Auto rule for `listingupdates@flexmail.flexmls.com` with subject `Copy: Subscription Investing`.
- To monitor more than one mailbox, add entries under `email.accounts`. Each account can use a different IMAP host and folder, while still inheriting the top-level city and sender filters if you leave those fields out.
- Google Sheet city matching only checks column A of each row.
- Telegram bot credentials (`telegram.bot_token`, `telegram.chat_id`).
- Google Sheet source details (public link or service account).

## Telegram setup

1. In Telegram, message `@BotFather` and create a bot with `/newbot`.
2. Copy the bot token into `telegram.bot_token`.
3. Send any message to your new bot from your Telegram account.
4. Open `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`.
5. Find your `chat.id` in the JSON response and set `telegram.chat_id`.

## Google Sheets options

### Option A: Public Sheet (no credentials)

1. Set `gsheet.public_url` to your normal sheet URL (or set `gsheet.public_csv_url` directly).
2. Ensure the sheet/tab is viewable publicly.
3. Leave service account settings unused.

### Option B: Service Account (API auth)

1. Create a Google Cloud service account.
2. Enable Google Sheets API and Google Drive API.
3. Download the service-account JSON key.
4. Place it at `creds/google-service-account.json` (or update `credentials_json`).
5. Share the target Google Sheet with the service account email.

## Gmail prerequisites

For Gmail, use an App Password (not your regular password), and IMAP must be enabled.

## Run once

```powershell
python monitor.py
```

## GitHub Actions Automation (Secrets-only)

The workflow file is [`.github/workflows/monitor.yml`](.github/workflows/monitor.yml). It runs every 30 minutes from 8:00 AM to 6:00 PM America/Phoenix and also supports manual runs.

### 1) Add repository secrets

In GitHub: `Settings -> Secrets and variables -> Actions -> New repository secret`

Required:
- `EMAIL_USERNAME`
- `EMAIL_APP_PASSWORD`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Optional:
- `EMAIL_LOOKBACK_HOURS` (overrides the template's email lookback window for GitHub Actions runs)
- `ZOHO_EMAIL_USERNAME`
- `ZOHO_EMAIL_APP_PASSWORD`
- `ZOHO_IMAP_HOST` (defaults to `imap.zoho.com`)
- `ZOHO_FOLDER` (defaults to `Off-Market-Deals`)
- `ZOHO_LOOKBACK_HOURS` (defaults to the template lookback window when set)
- `CLOUD_CMA_API_KEY` (activates ARMLS comp-report requests and pre-screening)
- `GSHEET_PUBLIC_CSV_URL`
- `GSHEET_PUBLIC_URL`
- `GSHEET_SPREADSHEET_ID`
- `GSHEET_SERVICE_ACCOUNT_JSON` (full JSON string of the service-account key)

Google Sheet secret combinations:
- Public-sheet mode: set `GSHEET_PUBLIC_CSV_URL` or `GSHEET_PUBLIC_URL`.
- Service-account mode: set `GSHEET_SERVICE_ACCOUNT_JSON` and `GSHEET_SPREADSHEET_ID`.

### 2) Enable workflow write access

In GitHub: `Settings -> Actions -> General -> Workflow permissions`
- Select `Read and write permissions`

This is required so the workflow can auto-commit `state/monitor_state.json` when it changes.

### 3) What the workflow does

- Builds `config.yaml` from `config.example.yaml` plus GitHub Secrets.
- Keeps the template's email lookback by default, and optionally overrides it with `EMAIL_LOOKBACK_HOURS` for GitHub Actions runs.
- If `GSHEET_SERVICE_ACCOUNT_JSON` is set, writes it to `creds/google-service-account.json` at runtime.
- Runs `python monitor.py`.
- Commits `state/monitor_state.json` when updated.

### 4) Local development

- Keep real credentials out of committed `config.yaml`.
- For local runs, copy `config.example.yaml` to `config.yaml` and fill local values.
- `.gitignore` excludes `config.yaml` and `creds/*.json` from future commits.

## Schedule every 10 minutes (Windows Task Scheduler)

Use Task Scheduler to run:

- Program/script: `python`
- Add arguments: `monitor.py`
- Start in: `D:\repo\flip-auto`
- Trigger: Repeat task every `10 minutes` indefinitely.

## Notes

- If both `telegram.enabled` and `twilio.enabled` are `false`, the script prints matches instead of sending alerts.
- State is stored in `state/monitor_state.json`.
