# One-time GitHub to GCP shadow-secret transfer

This initializes three existing Secret Manager containers in project `flip-auto`
(number `941818435041`) from the values used by the existing GitHub monitor.
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
Zoho, Telegram, Cloud CMA API credentials, GitHub PATs and deployment tokens are
outside this transfer. The initial shadow job covers Gmail; add Zoho separately
before claiming parity across both mailboxes.

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
and enter `COPY-THREE-SHADOW-SECRETS` as the confirmation. Existing environment
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

## References

- [Google: deployment-pipeline federation](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines)
- [Google: Secret Manager roles and expiring IAM conditions](https://docs.cloud.google.com/secret-manager/docs/access-control)
- [Google authentication action](https://github.com/google-github-actions/auth)
- [GitHub: manually running a workflow](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow)
