#!/usr/bin/env bash
# Creates a PAUSED shadow schedule; this script never activates scheduling.
# Runs every 30 minutes from 07:00 through 18:00 America/Phoenix once resumed.
# After a partial failure, rerun with --apply --recover-existing-sa. Recovery
# requires this script's exact account metadata and no existing invocation IAM.
set -euo pipefail
[[ "${1:-}" == "--apply" && ( $# == 1 || ( $# == 2 && "$2" == "--recover-existing-sa" ) ) ]] || {
  printf 'Usage: bash deploy/gcp/create-paused-scheduler.sh --apply [--recover-existing-sa]\n' >&2
  exit 2
}
: "${FLIP_AUTO_GCP_PROJECT_ID:?Set the explicitly approved project ID}"
: "${FLIP_AUTO_GCP_REGION:?Set the approved region}"
[[ "$FLIP_AUTO_GCP_PROJECT_ID" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] || exit 2
[[ "$FLIP_AUTO_GCP_REGION" =~ ^[a-z]+-[a-z]+[0-9]+$ ]] || exit 2
scheduler_sa="flip-auto-shadow-scheduler@${FLIP_AUTO_GCP_PROJECT_ID}.iam.gserviceaccount.com"
marker='Managed by flip-auto/create-paused-scheduler.sh'
work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT
if [[ "${2:-}" == '--recover-existing-sa' ]]; then
  gcloud iam service-accounts describe "$scheduler_sa" \
    --project="$FLIP_AUTO_GCP_PROJECT_ID" --format=json > "$work_dir/account.json"
else
  # A pre-existing account stops normal setup; recovery must be explicit.
  gcloud iam service-accounts create flip-auto-shadow-scheduler \
    --project="$FLIP_AUTO_GCP_PROJECT_ID" --display-name='Flip Auto shadow invoker' \
    --description="$marker" --format=json > "$work_dir/account.json"
fi
gcloud projects get-iam-policy "$FLIP_AUTO_GCP_PROJECT_ID" \
  --format=json > "$work_dir/project-policy.json"
gcloud run jobs get-iam-policy flip-auto-shadow \
  --project="$FLIP_AUTO_GCP_PROJECT_ID" --region="$FLIP_AUTO_GCP_REGION" \
  --format=json > "$work_dir/job-policy.json"
gcloud scheduler jobs list --project="$FLIP_AUTO_GCP_PROJECT_ID" \
  --location="$FLIP_AUTO_GCP_REGION" --format=json > "$work_dir/schedulers.json"
# As with initial bootstrap, inherited folder/organization IAM must not grant
# invocation to this new identity. Check project/job grants before any creation.
scheduler_state="$(python3 - "$work_dir" "$FLIP_AUTO_GCP_PROJECT_ID" "$FLIP_AUTO_GCP_REGION" "$scheduler_sa" "$marker" <<'PY'
import json, pathlib, sys
root, project, region, account, marker = sys.argv[1:]
def read(name):
    return json.loads((pathlib.Path(root) / name).read_text())
def require(ok, message):
    if not ok:
        raise SystemExit(message)
sa = read('account.json')
require(sa.get('email') == account and sa.get('displayName') == 'Flip Auto shadow invoker'
        and sa.get('description') == marker and not sa.get('disabled', False),
        'Account identity/metadata mismatch; no scheduler changes made.')
for filename in ('project-policy.json', 'job-policy.json'):
    for binding in read(filename).get('bindings', []):
        for member in binding.get('members', []):
            broad = member in ('allUsers', 'allAuthenticatedUsers') or (
                member.startswith('principalSet://cloudresourcemanager.googleapis.com/')
                and '/type/ServiceAccount' in member)
            require(member != 'serviceAccount:' + account and not broad,
                    'Existing account or broad IAM grant found; stop and inspect. '
                    'A completed PAUSED setup does not need to be rerun.')
name = f'projects/{project}/locations/{region}/jobs/flip-auto-shadow'
jobs = [job for job in read('schedulers.json') if job.get('name', '').endswith('/jobs/flip-auto-shadow')]
require(len(jobs) <= 1, 'Ambiguous scheduler identity; no scheduler changes made.')
if jobs:
    job = jobs[0]
    target = job.get('httpTarget', {})
    retry = job.get('retryConfig', {})
    headers = {key.lower(): value for key, value in target.get('headers', {}).items()}
    require(job.get('name') == name and job.get('description') == marker
            and job.get('schedule') == 'every 30 minutes from 07:00 to 18:00' and job.get('timeZone') == 'America/Phoenix'
            and job.get('state') in ('PAUSED', 'ENABLED') and job.get('attemptDeadline') == '180s'
            and retry.get('retryCount', 0) == 0 and retry.get('maxRetryDuration', '0s') == '0s'
            and headers.get('content-type') == 'application/json'
            and target.get('uri') == f'https://run.googleapis.com/v2/projects/{project}/locations/{region}/jobs/flip-auto-shadow:run'
            and target.get('httpMethod') == 'POST' and target.get('body') == 'e30='
            and target.get('oauthToken') == {'serviceAccountEmail': account, 'scope': 'https://www.googleapis.com/auth/cloud-platform'},
            'Existing scheduler does not match this setup; no scheduler changes made.')
print(jobs[0]['state'] if jobs else 'ABSENT')
PY
)"
if [[ "$scheduler_state" == ABSENT ]]; then
  for attempt in {1..7}; do
    if gcloud scheduler jobs create http flip-auto-shadow \
      --project="$FLIP_AUTO_GCP_PROJECT_ID" --location="$FLIP_AUTO_GCP_REGION" \
      --description="$marker" --schedule='every 30 minutes from 07:00 to 18:00' --time-zone=America/Phoenix \
      --uri="https://run.googleapis.com/v2/projects/${FLIP_AUTO_GCP_PROJECT_ID}/locations/${FLIP_AUTO_GCP_REGION}/jobs/flip-auto-shadow:run" \
      --http-method=POST --headers=Content-Type=application/json --message-body='{}' \
      --oauth-service-account-email="$scheduler_sa" \
      --oauth-token-scope=https://www.googleapis.com/auth/cloud-platform \
      --attempt-deadline=180s --max-retry-attempts=0 --max-retry-duration=0s \
      2> "$work_dir/create.err"; then
      break
    fi
    failure="$(cat "$work_dir/create.err")"
    if [[ $attempt == 7 || "$failure" != *"$scheduler_sa"* || ! "${failure,,}" =~ (does.not.exist|doesn.t.exist|not.found) ]]; then
      printf '%s\nSetup stopped; inspect the error, then use --apply --recover-existing-sa for partial setup.\n' "$failure" >&2
      exit 1
    fi
    printf 'Waiting 15 seconds for the new service account to propagate (attempt %s/7).\n' "$attempt" >&2
    sleep 15
  done
fi
if [[ "$scheduler_state" != PAUSED ]]; then
  gcloud scheduler jobs pause flip-auto-shadow \
    --project="$FLIP_AUTO_GCP_PROJECT_ID" --location="$FLIP_AUTO_GCP_REGION"
fi
scheduler_state="$(gcloud scheduler jobs describe flip-auto-shadow \
  --project="$FLIP_AUTO_GCP_PROJECT_ID" --location="$FLIP_AUTO_GCP_REGION" --format='value(state)')"
[[ "$scheduler_state" == 'PAUSED' ]] || {
  printf 'Scheduler not confirmed PAUSED; no invocation permission granted. Stop.\n' >&2
  exit 1
}
gcloud run jobs add-iam-policy-binding flip-auto-shadow \
  --project="$FLIP_AUTO_GCP_PROJECT_ID" --region="$FLIP_AUTO_GCP_REGION" \
  --member="serviceAccount:${scheduler_sa}" --role=roles/run.invoker
printf 'Scheduler is PAUSED. Invocation permission is scoped to the shadow job.\n'
printf 'Resume scheduling separately after reviewing the shadow comparison window.\n'
