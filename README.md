# flip-auto monitor

This project includes a Python program (`monitor.py`) that:

- Checks IMAP email for configured sender addresses.
- Reads email subject/body and matches configured cities.
- Checks a Google Sheet for matching city rows.
- Sends Telegram alerts for new matches (Twilio optional fallback).
- Stores sent IDs in a local state file to prevent duplicate texts.

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
- `email.sender_filters` for allowed senders.
- `email.lookback_hours` or `email.lookback_minutes` for how far back IMAP email should be scanned.
- `email.cities` and `gsheet.cities` for target cities.
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
- `GSHEET_PUBLIC_URL`
- `GSHEET_SPREADSHEET_ID`
- `GSHEET_SERVICE_ACCOUNT_JSON` (full JSON string of the service-account key)

Google Sheet secret combinations:
- Public-sheet mode: set `GSHEET_PUBLIC_URL`.
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
