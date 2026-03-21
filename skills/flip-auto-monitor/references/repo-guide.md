# Repo Guide

## File map

- `monitor.py`: IMAP scan, Google Sheet scan, dedupe, and notifier dispatch.
- `config.example.yaml`: committed config template and supported keys.
- `config.yaml`: local runtime config; treat secrets as sensitive.
- `state/monitor_state.json`: dedupe store of previously sent alert IDs.
- `.github/workflows/monitor.yml`: scheduled GitHub Actions run that builds config from secrets.
- `sample_carter.eml`: local Carter fixture for Flagstaff parsing and dedupe checks.

## Common tasks

### Check whether a sample email would trigger

Use inline Python with `message_from_binary_file`, then call:

- `parse_email_subject`
- `parse_email_body`
- `detect_email_city`
- `stable_id`
- `load_state`

This answers "would it match" and "would it be suppressed as already seen" without sending alerts.

### Run the monitor live

Run:

```powershell
.\.venv\Scripts\python.exe monitor.py
```

Only do this when the user explicitly wants the live path. It can send real Telegram or Twilio messages and will update the state file.

### Add or change config

When adding config such as a new lookback key:

1. Update `monitor.py`.
2. Update `config.example.yaml`.
3. Update `README.md`.
4. Update `.github/workflows/monitor.yml` if the workflow constructs or overrides that setting.

## Validation tips

- Prefer `.\.venv\Scripts\python.exe` so the repo's installed dependencies are used.
- If console output contains emoji, reconfigure stdout to UTF-8 in inline Python.
- Report whether a change affects local runs only, workflow runs only, or both.
