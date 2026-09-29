# Production cutover: GitHub Actions to GCP

The September 28 shadow window verified eight consecutive successful Gmail and
Zoho scans, with zero scan errors. That verifies mailbox access, not a live
CMA-to-Telegram cycle. The production path is a separate, initially disabled
runtime. Keep the existing GitHub monitor enabled throughout preparation.

This runbook deliberately keeps the existing `flip-auto-shadow` job, schedule,
service account, database, and secret versions unchanged. The Gmail sender list
includes `info@rezamp.com` in the new image. Moving Gmail to a label is a separate
change; this cutover preserves current mailbox selection.

## Fixed production resources

| Resource | Configuration |
| --- | --- |
| Project | `flip-auto` (`941818435041`) |
| Region | `us-central1` |
| Runtime service account | `flip-auto-live@flip-auto.iam.gserviceaccount.com` |
| Firestore database | `flip-auto-live`, Native/Standard, deletion protection enabled |
| State document | `flip_auto_live_state/monitor`; `state_json` indexing disabled |
| Cloud Run job | `flip-auto-live`; one task, parallelism one, zero retries, 900-second timeout, one CPU, 1Gi |
| Runtime command | `python gcp_live_runtime.py`; the image default remains shadow |
| Scheduler identity | `flip-auto-live-scheduler@flip-auto.iam.gserviceaccount.com` |
| Scheduler | `flip-auto-live`, created PAUSED; every 30 minutes, 07:00–18:00 America/Phoenix |

The runtime gets `roles/datastore.user` with a condition matching only the live
database, and `roles/secretmanager.secretAccessor` on nine individual secrets.
The scheduler gets `roles/run.invoker` only on the live job, after its PAUSED
state is verified. The preparation tool rejects broad project/job grants and
unexpected existing resources. An operator must also confirm organization/folder
policies do not independently grant the new scheduler identity invocation rights.
No service-account keys are created.

## 1. Prepare infrastructure while GitHub remains primary

Use a clean checkout pinned to the reviewed merge commit for this change. Run the
following from the repository root in authenticated Cloud Shell:

```bash
python3 deploy/gcp/prepare-live.py --check
python3 deploy/gcp/prepare-live.py --prepare
```

The check is read-only. Prepare creates only the new live database, runtime
identity, four new Secret Manager containers, index exemption, and scoped IAM.
It reads no secret values and does not create or execute workloads. A repeat
verifies matching resources and adds only missing resources/bindings. An existing
resource with conflicting metadata stops the operation; do not delete resources
to bypass that refusal.

## 2. Transfer the four production settings/secrets once

The five Gmail, Zoho, and webhook secrets already exist. Do not rerun the completed
core or Zoho transfers. The new production profile transfers only:

| Destination secret | Runtime environment variable |
| --- | --- |
| `flip-auto-cloud-cma-api-key` | `CLOUD_CMA_API_KEY` |
| `flip-auto-telegram-bot-token` | `TELEGRAM_BOT_TOKEN` |
| `flip-auto-telegram-chat-id` | `TELEGRAM_CHAT_ID` |
| `flip-auto-live-settings` | `FLIP_AUTO_LIVE_SETTINGS_JSON` |

The last item is an in-memory settings bundle derived from the current GitHub
monitor's inputs: lookback windows, Zoho host/folder, and public sheet URL
configuration. The workflow validates these settings before cloud authentication.
The expected Zoho endpoint remains `imappro.zoho.com` with SSL/993.

Choose one explicit UTC deadline in the future and at most 48 hours away; keep it
unchanged on retries. Do not reuse an expired deadline from an earlier transfer.
Set `flip_auto_transfer_expiry` to that chosen timestamp, then:

```bash
python3 deploy/gcp/setup-secret-transfer.py --profile production --check \
  --expires-at "$flip_auto_transfer_expiry"
python3 deploy/gcp/setup-secret-transfer.py --profile production --apply \
  --expires-at "$flip_auto_transfer_expiry"
```

Start a **new** GitHub Actions **Transfer production secrets to GCP** workflow on
`main`, with confirmation `COPY-PRODUCTION-SECRETS`. Verify all four upload steps
and retain the four printed numeric destination version IDs. Each new transfer
adds versions; inspect an interrupted run before retrying. Do not expose secret
values in terminal output, chat, files, or logs. Revoke immediately after success:

```bash
python3 deploy/gcp/setup-secret-transfer.py --profile production --revoke
```

The dedicated production transfer provider and per-secret version-adder grants
are separate from both completed transfers. Revocation does not disable the
runtime's secret access.

## 3. Build and verify the pinned live image

Run the Python and Worker tests from the reviewed checkout before building:

```bash
python3 -m unittest discover -s tests -v
node --test worker/test/index.test.mjs
```

The environment needs `requirements-gcp.txt` installed. CI runs these same gates.
Use a unique image tag tied to the reviewed commit; `flip_auto_image_tag` below
must be `us-central1-docker.pkg.dev/flip-auto/flip-auto/monitor:<unique-tag>`.

```bash
gcloud auth configure-docker us-central1-docker.pkg.dev --quiet
docker build --platform=linux/amd64 --tag "$flip_auto_image_tag" .
docker run --rm --read-only --network=none --cap-drop=ALL \
  --security-opt=no-new-privileges --entrypoint=python "$flip_auto_image_tag" \
  -c 'import gcp_live_runtime as r; assert r.PROJECT_ID == "flip-auto"; assert r.DATABASE_ID == "flip-auto-live"; assert r.STATE_COLLECTION == "flip_auto_live_state"; print("Live runtime import and fixed destination verified")'
docker image inspect "$flip_auto_image_tag" \
  --format='{{json .Config.Entrypoint}}'
docker push "$flip_auto_image_tag"
docker image inspect "$flip_auto_image_tag" --format='{{json .RepoDigests}}'
```

The entrypoint must still be `["python","gcp_runtime.py"]`. Set
`flip_auto_live_image` to the returned repository URL with its full `@sha256:`
digest. The deployment helper rejects tags and images from another repository.
The Docker allowlist contains code and the template only; it does not include
state snapshots, private configuration, uploaded emails, or credentials.

Create `versions.json` with exactly these nine keys and **actual numeric version
strings**, using the current shadow bindings for the first five and the verified
production transfer output for the remaining four:

```json
{
  "EMAIL_USERNAME": "<verified numeric version>",
  "EMAIL_APP_PASSWORD": "<verified numeric version>",
  "CLOUD_CMA_WEBHOOK_SECRET": "<verified numeric version>",
  "ZOHO_EMAIL_USERNAME": "<verified numeric version>",
  "ZOHO_EMAIL_APP_PASSWORD": "<verified numeric version>",
  "CLOUD_CMA_API_KEY": "<verified numeric version>",
  "TELEGRAM_BOT_TOKEN": "<verified numeric version>",
  "TELEGRAM_CHAT_ID": "<verified numeric version>",
  "FLIP_AUTO_LIVE_SETTINGS_JSON": "<verified numeric version>"
}
```

These placeholders are intentionally invalid. Do not assume any version is `1`
and do not use `latest`. Inspect the current shadow job JSON and Secret Manager
version **metadata** to establish the first five pins. The job creation helper
checks that all nine exact versions are `ENABLED`; it never reads their payloads.
Set `flip_auto_callback_base` to the existing callback Worker's HTTPS base URL,
without a path or token. Set `flip_auto_webhook_version` to the same numeric
`CLOUD_CMA_WEBHOOK_SECRET` version recorded above.

```bash
python3 deploy/gcp/prepare-live.py --check \
  --image "$flip_auto_live_image" --versions versions.json \
  --callback-base-url "$flip_auto_callback_base"
python3 deploy/gcp/prepare-live.py --create-job \
  --image "$flip_auto_live_image" --versions versions.json \
  --callback-base-url "$flip_auto_callback_base"
```

This creates the live job and a **PAUSED** scheduler. Creation never executes the
job. Existing exact matches are reusable; drift is refused, not overwritten.
If setup stops after scheduler creation, repeating the same command can pause
that exact scheduler while its invoker grant is still absent. Once invocation
permission exists, an active scheduler is refused rather than silently changed.

## 4. Deploy callback support, then establish a single live owner

Merging changes under `worker/` automatically starts the existing **Deploy Cloud
CMA Callback Worker** workflow. Verify that run deployed the reviewed merge
commit; if necessary start a fresh manual run of that workflow on `main`. Keep
the current KV namespace, webhook secret and Worker URL. Check that deployment
reused `flip-auto-cma-results` and did not create a replacement namespace. The
Worker initially retains GitHub dispatch behavior. The existing setup is
documented in [the callback Worker section](../README.md#cloud-cma-callback-worker).
Confirm deployment success before pausing production.

Now disable only the **production monitor** GitHub Actions workflow in the GitHub
UI, and wait for all queued/in-progress monitor runs to finish. Keep unit tests
and secret-transfer workflows available. The cutover tool independently verifies
the production workflow is disabled and no nonterminal monitor runs exist.

Switch the existing Worker to polling only:

```bash
python3 deploy/gcp/live-cutover.py --callback-poll \
  --callback-base-url "$flip_auto_callback_base" \
  --webhook-secret-version "$flip_auto_webhook_version"
```

The control mode is stored in Cloudflare KV, whose propagation is asynchronous.
Disabling and draining GitHub is the ownership fence, not a single KV readback.
Callbacks remain stored for polling; old or new callbacks must not invoke an
active second production runner. The tool reads only the pinned webhook secret
into memory to authenticate callback controls; it never prints the value.

Import the **latest final GitHub production state**, not the shadow database:

```bash
python3 deploy/gcp/live-cutover.py --import-state \
  --image "$flip_auto_live_image" --versions versions.json \
  --callback-base-url "$flip_auto_callback_base" \
  --webhook-secret-version "$flip_auto_webhook_version"
python3 deploy/gcp/live-cutover.py --inspect
```

Import verifies the successful final monitor run and source state provenance,
validates the full state, and creates the live document only if absent. The
imported document contains `enabled: false`, no lease, and no in-flight effect.
It preserves seen messages, prior notifications, pending CMA requests and other
tracking data, so existing deals are not replayed as new work. Partial imports,
state drift, active leases and ambiguous external effects stop the cutover.
Reports already deleted by the GitHub runner cannot be restored by importing
state. A pending request or changed asking price may still reference such a
report until its existing request TTL expires; inspect that case without
clearing request history. New GCP processing retains callback reports for seven
days.

## 5. Activate a controlled live execution and verify the full cycle

After the import checks pass:

```bash
python3 deploy/gcp/live-cutover.py --activate \
  --image "$flip_auto_live_image" --versions versions.json \
  --callback-base-url "$flip_auto_callback_base" \
  --webhook-secret-version "$flip_auto_webhook_version"
gcloud run jobs execute flip-auto-live --wait \
  --project=flip-auto --region=us-central1
```

Activation enables the state gate but keeps the scheduler paused. This first job
is **live**: it may request CMAs and send qualifying Telegram alerts. It preserves
existing filtering, screening, and deduplication. An empty/no-alert successful run
alone is not evidence of the full live path. Inspect Cloud Run's completion,
Gmail and Zoho summaries, zero scan errors, the relevant CMA request and callback,
and receipt of the resulting Telegram alert. A callback can require a second
controlled execution after the report arrives. Do not fabricate a passing alert
or clear existing deduplication state to force one.

```bash
python3 deploy/gcp/live-cutover.py --inspect
gcloud run jobs executions list --job=flip-auto-live \
  --project=flip-auto --region=us-central1 --limit=5 \
  --sort-by='~metadata.creationTimestamp'
```

Only after that real CMA → callback → alert evidence is reviewed, resume with the
explicit confirmation required by the tool:

```bash
python3 deploy/gcp/live-cutover.py --resume-schedule \
  --image "$flip_auto_live_image" --versions versions.json \
  --callback-base-url "$flip_auto_callback_base" \
  --webhook-secret-version "$flip_auto_webhook_version" \
  --confirmation CMA-CALLBACK-ALERT-VERIFIED
```

The tool rechecks GitHub ownership, job bindings, callback polling mode, a
successful live execution with the same image and all nine secret pins, matching
state execution identity/timestamps, zero errors, and no unresolved lease/effect
marker.
GitHub remains disabled. Cloudflare stores callbacks for seven days; polling
processes them on the next run. With this daytime-only schedule, a callback after
18:00 can wait until 07:00 Arizona the next morning. Immediate callback-driven
execution is not part of this migration.

## Failure recovery and rollback

If an external request's outcome is uncertain, the live runtime retains its
in-flight marker and blocks further external effects. Do not erase that marker
or retry the request blindly. Inspect the provider outcome first and reconcile
state before continuing. A 20-minute lease outlives the 15-minute task timeout;
wait for the existing execution/lease to end rather than forcing overlap.

To stop new scheduled work and disable new effects:

```bash
python3 deploy/gcp/live-cutover.py --pause
```

A running external operation can still complete; drain it and inspect state.
**Do not simply re-enable GitHub with its old `state/monitor_state.json`.** After any live GCP
processing, its state is the authoritative production state. A rollback requires
exporting and reconciling that state, resolving any in-flight request/alert, and
updating GitHub's production state before restoring callback dispatch and
re-enabling the workflow. `--callback-github` requires a paused GCP scheduler,
disabled live state and no active lease/effect; it does not enable GitHub itself.
Never run both production schedulers simultaneously.

References: [Cloud Run job creation](https://docs.cloud.google.com/sdk/gcloud/reference/run/jobs/create),
[Firestore database creation](https://docs.cloud.google.com/sdk/gcloud/reference/firestore/databases/create),
[field index exemption](https://docs.cloud.google.com/sdk/gcloud/reference/firestore/indexes/fields/update),
[Scheduler HTTP configuration](https://docs.cloud.google.com/sdk/gcloud/reference/scheduler/jobs/create/http).
