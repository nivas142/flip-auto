#!/usr/bin/env bash
# Run only after a successful manual shadow execution and explicit approval.
# Scheduler has no create-in-PAUSED API; withhold invocation IAM until paused.
set -euo pipefail
[[ "${1:-}" == "--apply" ]] || {
  printf 'Usage: bash deploy/gcp/create-paused-scheduler.sh --apply\n' >&2
  exit 2
}
: "${FLIP_AUTO_GCP_PROJECT_ID:?Set the explicitly approved project ID}"
: "${FLIP_AUTO_GCP_REGION:?Set the approved region}"
[[ "$FLIP_AUTO_GCP_PROJECT_ID" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] || exit 2
[[ "$FLIP_AUTO_GCP_REGION" =~ ^[a-z]+-[a-z]+[0-9]+$ ]] || exit 2

# Must be new: an existing account causes a stop before creating a schedule.
gcloud iam service-accounts create flip-auto-shadow-scheduler \
  --project="$FLIP_AUTO_GCP_PROJECT_ID" --display-name='Flip Auto shadow invoker'
scheduler_sa="flip-auto-shadow-scheduler@${FLIP_AUTO_GCP_PROJECT_ID}.iam.gserviceaccount.com"
gcloud scheduler jobs create http flip-auto-shadow \
  --project="$FLIP_AUTO_GCP_PROJECT_ID" --location="$FLIP_AUTO_GCP_REGION" \
  --schedule='*/30 * * * *' --time-zone=America/Phoenix \
  --uri="https://run.googleapis.com/v2/projects/${FLIP_AUTO_GCP_PROJECT_ID}/locations/${FLIP_AUTO_GCP_REGION}/jobs/flip-auto-shadow:run" \
  --http-method=POST --headers=Content-Type=application/json --message-body='{}' \
  --oauth-service-account-email="$scheduler_sa" \
  --oauth-token-scope=https://www.googleapis.com/auth/cloud-platform \
  --attempt-deadline=180s --max-retry-attempts=0 --max-retry-duration=0s
gcloud scheduler jobs pause flip-auto-shadow \
  --project="$FLIP_AUTO_GCP_PROJECT_ID" --location="$FLIP_AUTO_GCP_REGION"
scheduler_state="$(gcloud scheduler jobs describe flip-auto-shadow \
  --project="$FLIP_AUTO_GCP_PROJECT_ID" --location="$FLIP_AUTO_GCP_REGION" --format='value(state)')"
[[ "$scheduler_state" == "PAUSED" ]] || {
  printf 'Scheduler not confirmed PAUSED; no invocation permission granted. Stop.\n' >&2
  exit 1
}
gcloud run jobs add-iam-policy-binding flip-auto-shadow \
  --project="$FLIP_AUTO_GCP_PROJECT_ID" --region="$FLIP_AUTO_GCP_REGION" \
  --member="serviceAccount:${scheduler_sa}" --role=roles/run.invoker
printf 'Scheduler is PAUSED. Invocation permission is scoped to the shadow job.\n'
