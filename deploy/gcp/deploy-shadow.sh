#!/usr/bin/env bash
# Phase 1 only. No API enablement, secret transfer, execution, or scheduling.
set -euo pipefail

action="${1:---check}"
if [[ "$action" != "--check" && "$action" != "--apply" ]]; then
  printf 'Usage: bash deploy/gcp/deploy-shadow.sh [--check|--apply]\n' >&2
  exit 2
fi
: "${FLIP_AUTO_GCP_PROJECT_ID:?Set the explicitly approved project ID}"
: "${FLIP_AUTO_GCP_REGION:?Set the approved region (proposed: us-central1)}"
: "${FLIP_AUTO_IMAGE_DIGEST:?Set the Artifact Registry image URL with @sha256 digest}"
: "${CLOUD_CMA_CALLBACK_BASE_URL:?Set the existing Worker base URL, without a token}"
: "${EMAIL_USERNAME_SECRET_VERSION:?Set numeric flip-auto-email-username version}"
: "${EMAIL_PASSWORD_SECRET_VERSION:?Set numeric flip-auto-email-app-password version}"
: "${WEBHOOK_SECRET_VERSION:?Set numeric flip-auto-cma-webhook-secret version}"

[[ "$FLIP_AUTO_GCP_PROJECT_ID" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] || exit 2
[[ "$FLIP_AUTO_GCP_REGION" =~ ^[a-z]+-[a-z]+[0-9]+$ ]] || exit 2
image_prefix="${FLIP_AUTO_GCP_REGION}-docker.pkg.dev/${FLIP_AUTO_GCP_PROJECT_ID}/flip-auto/monitor@sha256:"
[[ "$FLIP_AUTO_IMAGE_DIGEST" == "$image_prefix"* ]] || exit 2
image_hash="${FLIP_AUTO_IMAGE_DIGEST#"$image_prefix"}"
[[ "$image_hash" =~ ^[a-f0-9]{64}$ ]] || exit 2
# Only a base URL is accepted; no comma/query credentials in gcloud env syntax.
[[ "$CLOUD_CMA_CALLBACK_BASE_URL" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?/?$ ]] || exit 2
for secret_version in "$EMAIL_USERNAME_SECRET_VERSION" "$EMAIL_PASSWORD_SECRET_VERSION" "$WEBHOOK_SECRET_VERSION"; do
  [[ "$secret_version" =~ ^[1-9][0-9]*$ ]] || exit 2
done

runtime_sa="flip-auto-shadow@${FLIP_AUTO_GCP_PROJECT_ID}.iam.gserviceaccount.com"
printf 'Validated shadow deployment target: project=%s region=%s job=flip-auto-shadow\n' \
  "$FLIP_AUTO_GCP_PROJECT_ID" "$FLIP_AUTO_GCP_REGION"
if [[ "$action" == "--check" ]]; then
  printf 'Local validation only. No Google API calls made. Add --apply only after the runbook gates.\n'
  exit 0
fi

gcloud run jobs create flip-auto-shadow \
  --project="$FLIP_AUTO_GCP_PROJECT_ID" --region="$FLIP_AUTO_GCP_REGION" \
  --image="$FLIP_AUTO_IMAGE_DIGEST" --service-account="$runtime_sa" \
  --tasks=1 --parallelism=1 --max-retries=0 --task-timeout=900s \
  --cpu=1 --memory=1Gi \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=${FLIP_AUTO_GCP_PROJECT_ID},FIRESTORE_DATABASE_ID=flip-auto,FLIP_AUTO_EXECUTION_MODE=shadow,CLOUD_CMA_CALLBACK_BASE_URL=${CLOUD_CMA_CALLBACK_BASE_URL}" \
  --set-secrets="EMAIL_USERNAME=flip-auto-email-username:${EMAIL_USERNAME_SECRET_VERSION},EMAIL_APP_PASSWORD=flip-auto-email-app-password:${EMAIL_PASSWORD_SECRET_VERSION},CLOUD_CMA_WEBHOOK_SECRET=flip-auto-cma-webhook-secret:${WEBHOOK_SECRET_VERSION}" \
  --labels=app=flip-auto,mode=shadow
printf 'Created shadow job; not executed. Scheduler has not been created.\n'
