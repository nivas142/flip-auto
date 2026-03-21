---
name: flip-auto-monitor
description: Maintain and debug the flip-auto property alert monitor repo. Use when Codex needs to modify or validate the IMAP email scanner, Google Sheets matcher, Telegram or Twilio alerting, state deduplication, sample email parsing, config templates, or the GitHub Actions workflow in this repository.
---

# Flip Auto Monitor

## Overview

Work from the repo's existing monitor flow instead of inventing a new one. Read only the files relevant to the task, preserve current behavior unless the user asks for a change, and treat `config.yaml` and any credentials as sensitive.

## Quick Start

Start by reading:

- `README.md` for the intended setup and deployment flow
- `monitor.py` for the actual runtime behavior
- `config.example.yaml` for supported config keys
- `.github/workflows/monitor.yml` for CI behavior when the task involves automation

Then choose the narrowest validation path that answers the user's request.

## Task Workflow

### Email match and parsing work

Use the repo's parser functions directly for most debugging:

- Load `sample_carter.eml` when the task is about Carter/Flagstaff matching.
- Reuse `parse_email_subject`, `parse_email_body`, `detect_email_city`, `stable_id`, and `load_state` instead of recreating logic.
- Prefer an inline Python check through `.\.venv\Scripts\python.exe` over editing code just to inspect a message.

If the task is "would this trigger", compute the `item_id` the same way `main()` does and compare it against the configured state file.

### Live runs

Assume `python monitor.py` may:

- connect to IMAP
- fetch Google Sheet data
- send Telegram or Twilio alerts
- mutate `state/monitor_state.json`

Do not run a live monitor pass unless the user explicitly asks for it. When the user does ask, say that it is a live run and report whether state changed.

### Safe validation

Prefer these validation levels, in order:

1. Function-level checks with inline Python.
2. Dry-run behavior by disabling notifiers in config when the task allows it.
3. Full live `monitor.py` only when the user explicitly wants the real integration path.

When a live run is unnecessary, avoid changing `config.yaml` permanently just to test something.

### Config and workflow changes

Keep config support aligned across:

- `monitor.py`
- `config.example.yaml`
- `README.md`
- `.github/workflows/monitor.yml` when automation depends on the new setting

If adding a new config key, document its precedence clearly when it overlaps an existing key.

## Repo Notes

- `monitor.py` is the single runtime entry point.
- State deduplication lives in `state/monitor_state.json`.
- The sample Carter email is the best local fixture for Flagstaff matching.
- The workflow builds `config.yaml` from `config.example.yaml` plus GitHub secrets, so workflow behavior can diverge from local config if the workflow overrides keys.

## References

Read `references/repo-guide.md` when you need a compact repo map, common verification commands, or reminders about the risky paths.
