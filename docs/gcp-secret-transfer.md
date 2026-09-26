# One-time GitHub to GCP shadow-secret transfer

This copies a fixed selection of credentials into existing Secret Manager containers
in project `flip-auto` (number `941818435041`) from the existing GitHub monitor.
The default `core` profile retains the original three-secret behavior. The explicit
`zoho` profile transfers only the two Zoho credentials using a different identity.
It does not read secret payloads back, change GitHub or Cloudflare credentials,
deploy a job, run the scanner, send alerts, or alter the production schedule.

| GitHub secret | GCP secret |
| --- | --- |
| `EMAIL_USERNAME` | `flip-auto-email-username` |
| `EMAIL_APP_PASSWORD` | `flip-auto-email-app-password` |
| `CLOUD_CMA_WEBHOOK_SECRET` | `flip-auto-cma-webhook-secret` |

The workflow uses the existing GitHub environment `main`, matching `monitor.yml`.
Environment secrets take precedence over repository secrets. All three values
must be nonempty before the first write. Missing names are reported without values.
The table above is the `core` profile. Telegram, Cloud CMA API credentials, GitHub
PATs and deployment tokens are outside both profiles. The initial shadow job covers
Gmail; the separate Zoho procedure below extends mailbox coverage.

**Core was completed and revoked on September 26, 2026. Do not repeat sections 1–3
for Zoho. Go directly to “Add Zoho after the completed core transfer.”**

## 1. Authorize this transfer from Cloud Shell

Use a reviewed checkout containing `deploy/gcp/setup-secret-transfer.py`. A PR
checkout can prepare GCP trust, but the transfer workflow cannot run until the
reviewed workflow has been merged into `main`.

The setup script targets only this project and checks its numeric ID. It reuses
only matching resources, rejects unexpected providers or broader existing grants,
and never needs a service-account JSON key. Use `--check` for a read-only
preflight. Choose a UTC expiry in the next 48 hours; use the same expiry on retries.
For this setup session, the window ends September 27, 2026 at 01:00 Arizona time:

```bash
python3 deploy/gcp/setup-secret-transfer.py --check \
  --expires-at '2026-09-27T08:00:00Z'
python3 deploy/gcp/setup-secret-transfer.py --apply \
  --expires-at '2026-09-27T08:00:00Z'
```

`--apply` enables the required IAM/STS/Secret Manager APIs, creates the dedicated
`flip-auto-secret-transfer` Workload Identity pool and `github` provider, and grants
`roles/secretmanager.secretVersionAdder` on each of the three secrets until the
specified expiry. The role cannot retrieve secret payloads or change IAM policies.
No project-wide role or service-account impersonation is granted.

GCP accepts only identity tokens with all of these claims:

- Repository `nivas142/flip-auto`, numeric repository ID `1172251948`.
- Repository owner ID `22221409`.
- Branch `refs/heads/main`.
- Workflow `nivas142/flip-auto/.github/workflows/transfer-gcp-secrets.yml@refs/heads/main`.
- Event `workflow_dispatch` and subject `repo:nivas142/flip-auto:environment:main`.

Allow a few minutes for new IAM grants to propagate before running the workflow.
If setup stops with an unexpected existing configuration, inspect it instead of
removing the checks or expanding the permissions.

## 2. Run once from the main branch

After merging the reviewed workflow, open GitHub Actions, select
**Transfer shadow secrets to GCP**, choose **Run workflow** with branch **main**,
select profile **core**, and enter `COPY-THREE-SHADOW-SECRETS` as the confirmation. Existing environment
protection rules still apply. There are no push, schedule, or pull-request triggers.

The workflow pins third-party actions to commit SHAs and checks out the triggering
commit. It provides the three secrets only to the transfer step. Payloads are sent
to `gcloud` through stdin; subprocess output is captured and errors are sanitized.
It uploads no artifacts, prints no values/hashes/tokens, and writes no secret files.
The authentication action removes its temporary credential file on completion.

Success logs contain only the three destination resource paths and their numeric
versions. Save those version numbers for the Cloud Run deployment. For previously
empty containers these should normally be version `1`, but use actual outputs.

## 3. Verify metadata, then revoke transfer access

In Cloud Shell, inspect version metadata only:

```bash
for secret_id in flip-auto-email-username flip-auto-email-app-password flip-auto-cma-webhook-secret; do
  gcloud secrets versions list "$secret_id" --project=flip-auto \
    --format='table(name,state,createTime)'
done
python3 deploy/gcp/setup-secret-transfer.py --revoke
```

Revocation disables the dedicated provider and removes its transfer-specific
expiring bindings. It leaves the runtime's `secretAccessor` grants, secret
versions, and all production resources intact. The expiry independently ends
write permission if cleanup is delayed.

Adding versions is not transactional across three secrets. A timeout can occur
after a version was saved. The helper does not retry writes automatically. If a
run fails after one or more writes, inspect version metadata before rerunning;
a rerun can add duplicate versions. Do not destroy earlier versions to hide this.
Deploy using the three explicitly reviewed numeric versions, never `latest`.

This setup has no authority to build images, deploy Cloud Run, or manage schedules.
Those operations use the existing Cloud Shell runbook or a separately reviewed
deployment identity. Secret transfer success does not demonstrate mailbox login
or callback access; the shadow execution tests those next.

## Add Zoho after the completed core transfer

The Zoho profile has these fixed destinations:

| GitHub secret | GCP secret |
| --- | --- |
| `ZOHO_EMAIL_USERNAME` | `flip-auto-zoho-email-username` |
| `ZOHO_EMAIL_APP_PASSWORD` | `flip-auto-zoho-email-app-password` |

It uses the new `flip-auto-zoho-secret-transfer` pool and its `github` provider.
The original `flip-auto-secret-transfer` provider remains disabled. Neither profile
can reactivate a disabled provider or accept arbitrary secret destinations. Both
use the exact repository, main branch, workflow, and environment claims above.

### 1. Prepare only the two new secrets and temporary trust

Use a reviewed checkout containing this version of both scripts and the workflow.
The runtime service account and shadow Cloud Run job must already exist. These
commands are for first-time creation of the two Zoho containers. If a container
already exists, stop and inspect its metadata instead of ignoring the error or
repeating the transfer.

```bash
(
  set -euo pipefail
  for secret_id in flip-auto-zoho-email-username flip-auto-zoho-email-app-password; do
    gcloud secrets create "$secret_id" --project=flip-auto \
      --replication-policy=automatic
    gcloud secrets add-iam-policy-binding "$secret_id" --project=flip-auto \
      --member=serviceAccount:flip-auto-shadow@flip-auto.iam.gserviceaccount.com \
      --role=roles/secretmanager.secretAccessor
  done
)
```

Choose one explicit UTC expiry within 48 hours and keep it unchanged on setup
retries. The following deadline is for the September 26, 2026 setup session;
replace it for a later session.

```bash
python3 deploy/gcp/setup-secret-transfer.py --profile zoho --check \
  --expires-at '2026-09-27T08:00:00Z'
python3 deploy/gcp/setup-secret-transfer.py --profile zoho --apply \
  --expires-at '2026-09-27T08:00:00Z'
```

Only `secretVersionAdder` is granted on those two containers. The runtime's
`secretAccessor` permissions are also scoped individually to those containers.
No existing secret versions, runtime settings, or production services change.

### 2. Transfer once with the Zoho profile

Merge the reviewed workflow into main, then dispatch **Transfer shadow secrets to
GCP** on **main**, select profile **zoho**, and confirm **COPY-TWO-ZOHO-SECRETS**.
Allow new IAM grants to propagate first. The core job is skipped, and no Gmail or
webhook secret is injected into the Zoho copy step.

Before GCP authentication, the workflow compares effective production settings to
the reviewed GCP settings:

| Setting | Required value |
| --- | --- |
| IMAP host | `imap.zoho.com` |
| Folder | `Off-Market-Deals` |
| Lookback | 48 hours |

Unset host/folder settings use production defaults. If `ZOHO_LOOKBACK_HOURS` is
unset, the check inherits `EMAIL_LOOKBACK_HOURS`, then defaults to 48, matching the
production monitor. This matters because the GCP runner defaults Zoho to 48 hours
independently of Gmail.

The preflight prints only `ZOHO_HOST_MATCH`, `ZOHO_FOLDER_MATCH`, and
`ZOHO_LOOKBACK_MATCH` with true/false values. Any mismatch stops before
GCP authentication or credential copying; review the intended nonsensitive
configuration before changing the check. Do not print or unmask GitHub secret
values to diagnose a mismatch. Both credentials must also be nonempty before any
upload. A successful transfer prints exactly the two version resource paths.

### 3. Revoke temporary transfer access, then bind pinned versions

Inspect metadata and revoke the **Zoho** profile, including after a failed copy:

```bash
for secret_id in flip-auto-zoho-email-username flip-auto-zoho-email-app-password; do
  gcloud secrets versions list "$secret_id" --project=flip-auto \
    --format='table(name,state,createTime)'
done
python3 deploy/gcp/setup-secret-transfer.py --profile zoho --revoke
```

A partial failure may already have added a version. Inspect the returned metadata
before deciding to retry; no retry is automatic. Revocation keeps secret versions
and runtime access intact, and never touches the completed core transfer.

After both credentials transferred successfully and configuration parity passed,
use the two reported numeric versions below. First confirm the existing job is
still the reviewed shadow job. This updates its next executions; it does not
start a separate execution or switch production alerts.

```bash
(
  set -euo pipefail
  ZOHO_USERNAME_SECRET_VERSION=REPLACE_WITH_REPORTED_NUMBER
  ZOHO_PASSWORD_SECRET_VERSION=REPLACE_WITH_REPORTED_NUMBER
  [[ "$ZOHO_USERNAME_SECRET_VERSION" =~ ^[1-9][0-9]*$ ]] || exit 2
  [[ "$ZOHO_PASSWORD_SECRET_VERSION" =~ ^[1-9][0-9]*$ ]] || exit 2

  gcloud run jobs update flip-auto-shadow \
    --project=flip-auto --region=us-central1 \
    --update-secrets="ZOHO_EMAIL_USERNAME=flip-auto-zoho-email-username:${ZOHO_USERNAME_SECRET_VERSION},ZOHO_EMAIL_APP_PASSWORD=flip-auto-zoho-email-app-password:${ZOHO_PASSWORD_SECRET_VERSION}" \
    --update-env-vars="ZOHO_IMAP_HOST=imap.zoho.com,ZOHO_FOLDER=Off-Market-Deals,ZOHO_LOOKBACK_HOURS=48"
)
```

Use `--update-secrets`, not `--set-secrets`, to retain the Gmail and callback
bindings. Do not rerun the create-only `deploy-shadow.sh`. The current shadow image
already supports the two Zoho bindings, so no rebuild is required for this update.
Confirm the next scheduled run scans both hosts with zero scan errors. Successful
mailbox scans still do not validate CMA valuation or authorize production cutover.

## References

- [Google: deployment-pipeline federation](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines)
- [Google: Secret Manager roles and expiring IAM conditions](https://docs.cloud.google.com/secret-manager/docs/access-control)
- [Google authentication action](https://github.com/google-github-actions/auth)
- [GitHub: manually running a workflow](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow)
