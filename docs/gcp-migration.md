# GCP migration: phase 1, shadow only

This is a deployment package, not a completed migration. Nothing in the existing
GitHub monitor schedule, Cloudflare callback Worker, or production state changes.
Do not run the cloud-writing commands below until the owner approves the exact
project, billing account, region, and credentials to transfer.

## Boundaries and success criteria

- Cloud Run Job runs `python gcp_runtime.py`; only `shadow` mode is supported.
- It reads the configured inbox and available CMA callbacks, calculates our ARV,
  and writes isolated shadow state. It never submits a CMA request, sends a
  Telegram/SMS alert, or deletes a callback. Claimed ARV remains excluded.
- Firestore Standard/Native database: `flip-auto`; collection:
  `flip_auto_shadow_state`; document: `monitor`. A transactional 20-minute lease
  protects that shadow document. No production state document is modified.
- One task, one parallel task, zero retries, 15-minute hard timeout, 1 CPU/1 GiB
  initial sizing. Measure memory on large PDFs before increasing it.
- Start with one manual cloud execution. Only then prepare a paused Scheduler;
  activate shadow scheduling after a separate review. Keep GitHub as the only
  production writer for at least 48 hours of comparable shadow evidence.
- A successful Scheduler request only confirms execution was started. Inspect
  the Cloud Run execution result and application summary to establish success.

Cloudflare can delete a result after the production consumer processes it, so
shadow may not observe every callback. Compare matched inputs and outcomes, not
just total alert counts. A controlled retained-report replay is needed when that
race prevents an apples-to-apples comparison.

## 1. Required decisions and permissions

The owner confirmed project ID `flip-auto` (number `941818435041`), enabled billing,
and created the registry and named database in `us-central1` on September 26, 2026.
The runtime service account, database-scoped permission, and three empty secret
containers with runtime access were also confirmed by Cloud Shell output.
Check actual cloud state before deployment. Review current prices
for Cloud Run Jobs, Firestore, Scheduler, Secret Manager, Artifact Registry,
logging, and outbound network traffic. Budget alerts notify; they do not cap
spend. This package does not create a budget or enable billing.

Use Cloud Shell with the operator's normal sign-in for the first deployment.
No service-account JSON key is needed. Use an authorized admin for one-time
API/resource/IAM setup; use scoped deploy permissions afterwards.

| Identity | Scope and role |
| --- | --- |
| `flip-auto-shadow` runtime SA | `roles/datastore.user`, conditioned to database `flip-auto` |
| Runtime SA | `roles/secretmanager.secretAccessor` on each selected secret only |
| `flip-auto-shadow-scheduler` SA | `roles/run.invoker` on `flip-auto-shadow` job only; granted only after PAUSED verification |
| Image publisher | `roles/artifactregistry.writer` on repository `flip-auto` |
| Job deployer | Cloud Run create/update permissions and `roles/iam.serviceAccountUser` on runtime SA |
| Scheduler operator | Scheduler create/pause/resume permissions and `roles/iam.serviceAccountUser` on scheduler SA |

Bootstrap requires permission to enable services, create service accounts,
repositories, databases and secrets, and change their IAM policies. Do not grant
Owner/Editor to runtime or scheduler identities. Keep Google's managed Cloud Run
and Cloud Scheduler service-agent roles intact; do not assign those roles to our
user-managed service accounts. Check inherited project/organization IAM: the new
scheduler SA must not receive invocation rights through a broad principal grant.

Firestore IAM supports database-level conditions, not collection/document-level
isolation for this server client. The runtime can access the whole named database.
Do not put unrelated or production data in it during shadow testing.

## 2. One-time setup (operator-run, after approval)

From the repository root, set explicit values. Do not change `gcloud` defaults:

```bash
export FLIP_AUTO_GCP_PROJECT_ID='flip-auto'
export FLIP_AUTO_GCP_REGION='us-central1'
gcloud projects describe "$FLIP_AUTO_GCP_PROJECT_ID"
gcloud billing projects describe "$FLIP_AUTO_GCP_PROJECT_ID"
```

The following commands create billable-capable resources. Run them individually
and stop on errors; if a named resource already exists, inspect it before reusing
it. Do not automatically replace it or broaden permissions.

```bash
gcloud services enable run.googleapis.com firestore.googleapis.com \
  secretmanager.googleapis.com artifactregistry.googleapis.com \
  cloudscheduler.googleapis.com iam.googleapis.com \
  --project="$FLIP_AUTO_GCP_PROJECT_ID"
gcloud artifacts repositories create flip-auto --repository-format=docker \
  --location="$FLIP_AUTO_GCP_REGION" --project="$FLIP_AUTO_GCP_PROJECT_ID"
gcloud firestore databases create --database=flip-auto --edition=standard \
  --type=firestore-native --location="$FLIP_AUTO_GCP_REGION" --delete-protection \
  --project="$FLIP_AUTO_GCP_PROJECT_ID"
gcloud firestore indexes fields update state_json --database=flip-auto \
  --collection-group=flip_auto_shadow_state --disable-indexes \
  --project="$FLIP_AUTO_GCP_PROJECT_ID"
gcloud iam service-accounts create flip-auto-shadow \
  --display-name='Flip Auto shadow runtime' --project="$FLIP_AUTO_GCP_PROJECT_ID"
gcloud projects add-iam-policy-binding "$FLIP_AUTO_GCP_PROJECT_ID" \
  --member="serviceAccount:flip-auto-shadow@${FLIP_AUTO_GCP_PROJECT_ID}.iam.gserviceaccount.com" \
  --role=roles/datastore.user \
  --condition="expression=resource.name=='projects/${FLIP_AUTO_GCP_PROJECT_ID}/databases/flip-auto',title=flip-auto-db-only"
```

`deploy/gcp/firestore.indexes.json` records the same field exemption for future
Firebase tooling. The `gcloud` command applies it directly without requiring
Firebase setup. No queries/indexes are needed for the single state document.

## 3. Transfer only the required secrets

Phase 1 needs these Secret Manager secrets (same project):

| Secret ID | Runtime environment variable |
| --- | --- |
| `flip-auto-email-username` | `EMAIL_USERNAME` |
| `flip-auto-email-app-password` | `EMAIL_APP_PASSWORD` |
| `flip-auto-cma-webhook-secret` | `CLOUD_CMA_WEBHOOK_SECRET` |

If the values exist only in GitHub Secrets, follow the
[one-time transfer instructions](gcp-secret-transfer.md). Otherwise add the
selected values in the Secret Manager console.
Do not paste credentials into this document, chat, git, shell command arguments,
Docker build arguments, logs, or source configuration. Record the numeric version
of each value; the deployment pins versions rather than using `latest`.

For each of the three exact secret IDs, grant runtime access:

```bash
for secret_id in flip-auto-email-username flip-auto-email-app-password flip-auto-cma-webhook-secret; do
  gcloud secrets add-iam-policy-binding "$secret_id" \
    --project="$FLIP_AUTO_GCP_PROJECT_ID" \
    --member="serviceAccount:flip-auto-shadow@${FLIP_AUTO_GCP_PROJECT_ID}.iam.gserviceaccount.com" \
    --role=roles/secretmanager.secretAccessor
done
```

Do not migrate the Cloud CMA API key, Telegram bot token, Twilio credentials,
Cloudflare deployment token, GitHub PAT, or Sheets service-account key for this
phase. GCP does not need these. Keep all existing production secrets in place.
The webhook secret currently authorizes callback reads and deletes at the Worker;
the shadow application prevents deletes, but this credential is not intrinsically
read-only. A dedicated read-only Worker token is a later hardening option.

Optional Zoho: create/grant separate username/password secrets and bind them to
`ZOHO_EMAIL_USERNAME` and `ZOHO_EMAIL_APP_PASSWORD` together. Do not enable an
unconfirmed account. Optional Sheets: only an already-approved public CSV URL is
supported in this phase; never make a private sheet public to accommodate it.

GitHub secret values cannot be downloaded through the normal secrets API. The
separate manual `transfer-gcp-secrets.yml` workflow copies only the three names
above using direct Workload Identity Federation and expiring write-only grants.
It uses the existing `main` GitHub environment. GCP also restricts the identity
to the exact repository IDs, main branch, workflow path, environment and manual
event. See the transfer instructions for setup, verification and revocation.
No transfer occurs on push, pull request, or merge. Do not create service-account keys.

## 4. Build and create the shadow Job

Run tests locally first. Build from the explicit Docker allowlist, which excludes
state, emails, PDFs, private configs, credentials, and git history.

```bash
python -m unittest discover -s tests -v
gcloud auth configure-docker "${FLIP_AUTO_GCP_REGION}-docker.pkg.dev"
export FLIP_AUTO_IMAGE_TAG="${FLIP_AUTO_GCP_REGION}-docker.pkg.dev/${FLIP_AUTO_GCP_PROJECT_ID}/flip-auto/monitor:shadow-v1"
docker build --platform=linux/amd64 -t "$FLIP_AUTO_IMAGE_TAG" .
docker push "$FLIP_AUTO_IMAGE_TAG"
gcloud artifacts docker images describe "$FLIP_AUTO_IMAGE_TAG" \
  --project="$FLIP_AUTO_GCP_PROJECT_ID" --format='value(image_summary.digest)'
```

Use the digest printed by that last command (not a mutable tag):

```bash
export FLIP_AUTO_IMAGE_DIGEST="${FLIP_AUTO_GCP_REGION}-docker.pkg.dev/${FLIP_AUTO_GCP_PROJECT_ID}/flip-auto/monitor@sha256:REPLACE_WITH_DIGEST"
export CLOUD_CMA_CALLBACK_BASE_URL='https://REPLACE_WITH_EXISTING_WORKER_HOST'
export EMAIL_USERNAME_SECRET_VERSION='1'
export EMAIL_PASSWORD_SECRET_VERSION='1'
export WEBHOOK_SECRET_VERSION='1'
bash deploy/gcp/deploy-shadow.sh --check
# Only after the project, IAM, image, and exact secret versions are reviewed:
bash deploy/gcp/deploy-shadow.sh --apply
```

`--check` is offline validation only: it does not prove IAM, enabled APIs, network,
or credentials. `--apply` creates the job but does not execute it or create a
schedule. It stops if the job exists; inspect existing configuration before using
an explicit `gcloud run jobs update` for later images.

## 5. Manual validation, then 48-hour shadow comparison

```bash
gcloud run jobs execute flip-auto-shadow --wait \
  --project="$FLIP_AUTO_GCP_PROJECT_ID" --region="$FLIP_AUTO_GCP_REGION"
gcloud run jobs executions list --job=flip-auto-shadow \
  --project="$FLIP_AUTO_GCP_PROJECT_ID" --region="$FLIP_AUTO_GCP_REGION"
gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="flip-auto-shadow"' \
  --project="$FLIP_AUTO_GCP_PROJECT_ID" --freshness=1h --limit=100
```

Validate: shadow mode in the summary; successful mailbox read; same allowed
cities/senders as production; report parse/valuation counts when available; no
requests, sends, or callback deletes; state saved under the shadow document;
lease released; overlapping execution safely refused with a nonzero exit. Check both IMAP providers
separately if Zoho is enabled. Cloud Run execution should finish within 15 minutes
without memory errors. An empty inbox is not an end-to-end validation.

For controlled replay, runtime accepts `FLIP_AUTO_INITIAL_STATE_PATH` for a
one-time approved snapshot mounted at runtime, not baked into the image. Without
an approved snapshot containing existing `cma_requests`, an empty shadow state
cannot discover those pending callbacks: fresh leads remain pending because
shadow cannot submit new requests. Mailbox-only success must not be reported as
valuation parity. A reviewed seed into the isolated, initially empty shadow
document is a prerequisite for useful callback replay; never overwrite existing
shadow state silently or copy shadow state back to production. State seeding is
not automated by these deployment scripts.

For a populated shadow document, use the separate
[retained CMA replay](gcp-cma-replay.md) to validate the deployed parser,
valuation, and screening without replacing state. The replay compares an exact
PDF and fixed inputs against a local production-source baseline; it does not
exercise new CMA submissions, callback delivery, or notifications. The
[Zoho transfer profile](gcp-secret-transfer.md#add-zoho-after-the-completed-core-transfer)
adds the second mailbox through separate, temporary two-secret access.

After the manual cloud execution passes, an operator can create the paused
scheduler with `bash deploy/gcp/create-paused-scheduler.sh --apply`. There is no
atomic create-as-PAUSED API: the script creates a fresh, unprivileged invoker,
creates the job, pauses it, verifies `PAUSED`, and only then grants job-specific
invocation. Stop if anything fails; do not manually grant invocation to an
unpaused job. It schedules every 30 minutes in `America/Phoenix` once resumed.

The setup retries brief service-account propagation failures. If a previous
attempt created the scheduler account but stopped before completion, rerun with
`--apply --recover-existing-sa`. Recovery accepts only the account and schedule
marked by this script, verifies their target settings and IAM, and never resumes
the schedule. Unexpected resources or broader invocation grants require review.
After a successful setup, do not rerun the helper; inspect or resume the existing
paused schedule instead.
This helper does not change the container image or production monitor.

After approving the shadow comparison window, activate it explicitly:

```bash
gcloud scheduler jobs resume flip-auto-shadow \
  --project="$FLIP_AUTO_GCP_PROJECT_ID" --location="$FLIP_AUTO_GCP_REGION"
```

Keep the existing GitHub/Cloudflare path live during the entire comparison.
Production cutover is blocked until: at least 48 hours of healthy shadow runs,
a fresh request/callback chain verified on the production path, matched-input
valuation parity, duplicate/price-update behavior checked, failure alerting
configured, credentials reviewed, a dedup-state transfer plan, and owner approval.
Shadow has no production-mode switch. A later change must coordinate scheduling,
callback delivery, state ownership, and sending so only one consumer has live
side effects. Cloudflare can remain the callback receiver initially.
The shadow period cannot prove a new GCP-originated CMA request/callback cycle;
that needs a later gated production test before declaring migration complete.

## Backout

Pause GCP Scheduler (if created); GitHub continues production unchanged:

```bash
gcloud scheduler jobs pause flip-auto-shadow \
  --project="$FLIP_AUTO_GCP_PROJECT_ID" --location="$FLIP_AUTO_GCP_REGION"
```

Pausing does not cancel an already-running Cloud Run execution. Inspect it and,
if necessary, cancel that exact execution in the console. Do not delete Firestore,
production state, or existing secrets. Review whether retained Artifact Registry,
Secret Manager, and Firestore resources still incur charges before later cleanup.

## Official references (checked September 26, 2026)

- [Cloud Run job creation](https://docs.cloud.google.com/run/docs/create-jobs)
- [Job secret configuration](https://docs.cloud.google.com/run/docs/configuring/jobs/secrets)
- [Schedule Cloud Run Jobs with OAuth](https://docs.cloud.google.com/run/docs/execute/jobs-on-schedule)
- [Scheduler Job API: state is output-only](https://docs.cloud.google.com/scheduler/docs/reference/rest/v1/projects.locations.jobs)
- [Named Firestore databases and database IAM conditions](https://docs.cloud.google.com/firestore/native/docs/manage-databases)
- [Disable single-field indexes](https://docs.cloud.google.com/sdk/gcloud/reference/firestore/indexes/fields/update)
- [Secret-scoped IAM](https://docs.cloud.google.com/secret-manager/docs/access-control)
- [Artifact Registry authentication and pushing](https://docs.cloud.google.com/artifact-registry/docs/docker/pushing-and-pulling)
