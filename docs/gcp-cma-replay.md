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

The PDF and four calculation modules are pinned by SHA256. The original module
files were checked against production source commit
`e8f1d5d7e86541cf7892d8da38c3ae8739d84480`. The replay recognizes exactly two
reviewed source profiles: that original deployed parser and the current
version-3 parser with compact PDF spacing support. The other three modules
must be identical. Results from either profile must match the same fixed local
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

## Recover an interrupted status or log read

If setup already created an execution and the final log read timed out, use the
same execution name from the output. Do not run `--apply` again. For example:

```bash
python3 deploy/gcp/run-cma-replay.py --inspect flip-auto-cma-replay-bqn2j
```

`--inspect` only reads the project, exact execution, and its result logs. It
checks the execution's image and prints task status before querying logs. An
omitted or mismatched image is reported separately and does not prevent the
bounded diagnostic read, but exact image verification is required for success. It
does not need the adjacent replay script, so a standalone reviewed copy of this
helper also supports recovery. It never creates, executes, updates, or deletes a
job, and never reads secrets or shadow state.

The log query is restricted to the execution's timestamps with a two-minute
margin, reads newest entries first, and has a 35-second timeout. It retries a
timeout or an empty log result once; permission failures are not retried. If logs
remain unavailable, the command exits nonzero and reports that baseline
verification is still unconfirmed. A successful task by itself is not proof that
the expected valuation and alert decisions were reproduced. A later `--inspect`
can recover the result without starting another Cloud Run execution.

## PDF parser compatibility check

The September 26 failed replay reached the result-hash comparison. Locally,
changing only pypdf from 6.10.0 to 6.19.0 reproduced a failure: the extracted
text joined street suffixes to cities and numeric values to labels, losing
comp addresses and years. This changed similarity weights and the upper ARV.
It was not a harmless JSON ordering difference. The subsequent Cloud Shell
check of the configured image confirmed pypdf 6.19.0 and Python 3.12.14. With
the fixed parser mounted, it reproduced the original result hash exactly,
including all four addresses/build years and both screening decisions. The
original failed Cloud Run execution itself has not been rerun successfully.

Parser version 3 handles these compact fields without accepting longer-word
label prefixes, list prices, or claimed ARV. It also rejects an explicitly
active listing even if historical sold fields appear on its page. The retained
PDF reproduces the original result hash under both pypdf versions with the fix.
New dependency installations pin pypdf 6.19.0; an existing image is unchanged.

From a reviewed checkout in Cloud Shell, run the fixed parser inside the exact
deployed image. This reports that image's actual Python/pypdf versions and tests
the fix without rebuilding or updating a Cloud Run job:

```bash
gcloud auth configure-docker us-central1-docker.pkg.dev --quiet
docker run --rm --read-only --cap-drop=ALL \
  --security-opt=no-new-privileges --workdir=/app --entrypoint=python \
  --mount "type=bind,src=$PWD/cloud_cma.py,dst=/app/cloud_cma.py,readonly" \
  --mount "type=bind,src=$PWD/scripts/gcp_cma_replay.py,dst=/app/gcp_cma_replay.py,readonly" \
  us-central1-docker.pkg.dev/flip-auto/flip-auto/monitor@sha256:90854306a03c5856b9b7b7bac550b399db4e6b8fcdd9d9843a7d4cafadd4fce3 \
  /app/gcp_cma_replay.py
```

The container receives no credentials or environment overrides. It downloads
only the retained PDF and calculates locally. The read-only mounts replace the
parser for this temporary container only, and the emitted `module_profile`
must be `current-parser`. Success requires `baseline_verified: true` and the
unchanged result hash below. This is a dependency compatibility check, not a
new successful Cloud Run execution or a deployment of the fix. Rebuild and
validate a separate candidate image before updating the scheduled shadow job.

On a result mismatch, the runner emits `[CMA_REPLAY_DIAGNOSTIC]` with explicit
selected comp facts, expected/actual hashes, and parser/dependency versions,
then exits nonzero. This record is never a success marker. It does not print
raw PDF text, email content, credentials, or environment variables.

## Put the verified parser into the shadow image

After the compatibility check succeeds, run from a reviewed checkout:

```bash
python3 deploy/gcp/update-shadow-parser.py --check
python3 deploy/gcp/update-shadow-parser.py --apply
```

This one-off helper verifies the exact parser and replay source hashes. It
builds a derivative of the original immutable image, copying only the fixed
`cloud_cma.py`; the tested dependencies and runtime remain the same. It runs
the retained-report replay against that candidate image with only the replay
script mounted, so the parser must be present in the image itself. A failed
replay stops the rollout before any Cloud Run update.

The helper verifies the fixed project and existing shadow job configuration,
pushes the validated image to the existing `monitor` repository, resolves an
immutable digest, checks for configuration drift, and updates only the image
on `flip-auto-shadow`. Readback must confirm the new image and preserved task
configuration. The old image digest is printed for rollback. It stops if the
job no longer uses the original image; rerunning is not a generic redeployment
or automatic rollback operation.

No schedule, IAM policy, secret version/binding, production workflow, replay
job, or shadow state is changed by this helper. It does not trigger a mailbox
execution. Check the next scheduled shadow execution for completion and scan
errors; deployment readback alone is not runtime validation. Continue Zoho
and request/callback validation separately before production cutover.

## Isolation

No container rebuild is required for the diagnostic replay. The helper uses the already deployed digest,
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
