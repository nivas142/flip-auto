# Retained CMA replay on GCP

This checks whether the deployed GCP calculation code reproduces a reviewed
local baseline from the same production source and PDF. It does not establish
the accuracy of an ARV, current property availability, new Cloud CMA request or
callback delivery, or successful Telegram delivery.

The fixed fixture uses the existing Arabian Drive report, a September 26, 2026
analysis date, and the same subject and cost inputs as the prior smoke test.
The report's map lists three bedrooms; the historical smoke-test input overrides
that with four. Both are recorded explicitly. This discrepancy is retained for
reproducibility and is not a verification of the property's bedroom count.

The PDF and four calculation modules are pinned by SHA256. The module files
were checked against production source commit
`e8f1d5d7e86541cf7892d8da38c3ae8739d84480`. Results must match the fixed local
baseline, including selected comps, ARV range, screening results, and alert
decisions. This is a comparison against the locally executed production source,
not a separately observed GitHub workflow result.

## Run once from a reviewed checkout

```bash
python3 deploy/gcp/run-cma-replay.py --check
python3 deploy/gcp/run-cma-replay.py --apply
```

`--check` compiles the adjacent replay script and validates command construction
offline. `--apply` verifies the project and deployed image, creates the separate
`flip-auto-cma-replay` job, starts one execution, waits for completion, and reads
only that execution's replay result logs. It stops if the replay job already
exists. Inspect that exact job before deciding whether another execution is
needed; do not recreate or update it blindly after an interrupted command.

No container rebuild is required. The helper uses the already deployed digest,
one task, one CPU, 1 GiB RAM, no task retries, and a 15-minute task timeout. It
overrides the new job's command with the reviewed replay script. This is necessary
because the normal image entrypoint starts the mailbox runner.

The replay job has no schedule or secret bindings. It uses the existing shadow
runtime service account without granting new IAM permissions. That identity
retains its existing permissions; the reviewed replay code does not access
Secret Manager, Firestore, mailboxes, callbacks, or notification APIs. Normal
shadow state is not read or written. A later cleanup can delete this diagnostic
job; the helper never deletes resources.

## Expected evidence

- One successful Cloud Run task and exactly one `[CMA_REPLAY]` result.
- `baseline_verified: true` and result hash
  `7907073ef91c53d371fd5bf0a29b9932ec3829c6598ef2fb5211b98638b2ad7e`.
- 59 PDF pages, four parsed and eligible closed comps.
- The real $378,000 ask reproduces `pass` and `would_notify: false`.
- The explicitly synthetic $250,000 ask reproduces `candidate` and
  `would_notify: true`. It sends no notification and is not an offer recommendation.

A changed PDF or module, missing/mismatched subject, unavailable valuation,
insufficient comps, altered screening result, or failed task must fail the
replay. Never substitute a zero-alert count for this evidence. The fixture uses
the existing 180-day production comparison window; it does not change the live
valuation rules or use claimed ARV.

Continue ordinary shadow runs and validate Zoho and the live request/callback
path separately before planning production cutover.
