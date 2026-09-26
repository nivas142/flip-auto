#!/usr/bin/env python3
"""Owner-run, temporary direct WIF access for a fixed GitHub transfer profile.

Uses only Python's standard library and the operator's authenticated gcloud.
Never reads secret values, creates keys/service accounts, or starts a workload.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
import re
import subprocess
import sys
import tempfile
import time

PROJECT = "flip-auto"
PROJECT_NUMBER = "941818435041"
POOL = "flip-auto-secret-transfer"
PROVIDER = "github"
POOL_RESOURCE = f"projects/{PROJECT_NUMBER}/locations/global/workloadIdentityPools/{POOL}"
PROVIDER_RESOURCE = f"{POOL_RESOURCE}/providers/{PROVIDER}"
MEMBER = f"principalSet://iam.googleapis.com/{POOL_RESOURCE}/attribute.repository_id/1172251948"
ROLE = "roles/secretmanager.secretVersionAdder"
SECRETS = (
    "flip-auto-email-username",
    "flip-auto-email-app-password",
    "flip-auto-cma-webhook-secret",
)
APIS = ("iam.googleapis.com", "sts.googleapis.com", "secretmanager.googleapis.com")
CLAIMS = {
    "repository_owner_id": "22221409",
    "repository_id": "1172251948",
    "repository": "nivas142/flip-auto",
    "ref": "refs/heads/main",
    "workflow_ref": "nivas142/flip-auto/.github/workflows/transfer-gcp-secrets.yml@refs/heads/main",
    "event_name": "workflow_dispatch",
    "sub": "repo:nivas142/flip-auto:environment:main",
}
MAPPING = {"google.subject": "assertion.sub", **{
    f"attribute.{claim}": f"assertion.{claim}" for claim in CLAIMS if claim != "sub"
}}
ATTRIBUTE_CONDITION = " && ".join(f"assertion.{key} == '{value}'" for key, value in CLAIMS.items())
CONDITION_TITLE = "flip-auto-secret-transfer-expiry"
POOL_DESCRIPTION = "One-time GitHub transfer of three Flip Auto secrets."
DESCRIPTION_PREFIX = "One-time GitHub secret transfer; expires-at="
RETRY_DELAYS = (1, 2, 4, 8, 16)


class TransferProfile:
    def __init__(self, pool, secrets, description):
        self.pool = pool
        self.secrets = secrets
        self.description = description
        self.pool_resource = f"projects/{PROJECT_NUMBER}/locations/global/workloadIdentityPools/{pool}"
        self.provider_resource = f"{self.pool_resource}/providers/{PROVIDER}"
        self.member = f"principalSet://iam.googleapis.com/{self.pool_resource}/attribute.repository_id/1172251948"


# Core stays byte-for-byte compatible with the completed original transfer.
# Zoho uses a separate identity; it never re-enables the revoked core provider.
PROFILES = {
    "core": TransferProfile(POOL, SECRETS, POOL_DESCRIPTION),
    "zoho": TransferProfile(
        "flip-auto-zoho-secret-transfer",
        ("flip-auto-zoho-email-username", "flip-auto-zoho-email-app-password"),
        "One-time GitHub transfer of two Flip Auto Zoho secrets.",
    ),
}


class SetupError(RuntimeError):
    pass


class GcloudError(SetupError):
    def __init__(self, args, stderr):
        self.not_found = bool(re.search(r"\bNOT_FOUND\s*:", stderr))
        super().__init__(f"gcloud {' '.join(args)} failed: {stderr.strip()}")


class Gcloud:
    def __init__(self, runner=None, sleeper=None):
        self.runner = runner or subprocess.run
        self.sleeper = sleeper or time.sleep

    def run(self, *args, missing_ok=False, retry_not_found=False):
        """Only explicit NOT_FOUND is absence; permission/network errors always fail."""
        for attempt in range(len(RETRY_DELAYS) + 1):
            result = self.runner(
                ["gcloud", *args, f"--project={PROJECT}", "--quiet", "--format=json", "--verbosity=error"],
                check=False, capture_output=True, text=True,
                env={**os.environ, "CLOUDSDK_CORE_DISABLE_PROMPTS": "1"},
            )
            if result.returncode == 0:
                try:
                    return json.loads(result.stdout) if result.stdout.strip() else None
                except json.JSONDecodeError as exc:
                    raise SetupError("gcloud returned invalid JSON; refusing to continue") from exc
            error = GcloudError(args, result.stderr)
            if error.not_found and retry_not_found and attempt < len(RETRY_DELAYS):
                self.sleeper(RETRY_DELAYS[attempt])
                continue
            if error.not_found and missing_ok:
                return None
            raise error


def validate_expiry(value, now=None):
    if not value or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
        raise SetupError("--expires-at must be an explicit UTC deadline: YYYY-MM-DDTHH:MM:SSZ")
    try:
        expiry = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise SetupError("--expires-at is not a valid UTC timestamp") from exc
    now = now or datetime.now(timezone.utc)
    if not now < expiry <= now + timedelta(hours=48):
        raise SetupError("--expires-at must be in the future and no more than 48 hours away")
    return value


def expiry_condition(expires_at):
    return {"title": CONDITION_TITLE, "expression": f"request.time < timestamp('{expires_at}')"}


def owned_condition(condition):
    return (
        isinstance(condition, dict)
        and condition.get("title") == CONDITION_TITLE
        and bool(re.fullmatch(
            r"request\.time < timestamp\('\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z'\)",
            condition.get("expression", ""),
        ))
    )


class Setup:
    def __init__(self, gcloud, profile="core"):
        if profile not in PROFILES:
            raise SetupError("Unknown transfer profile")
        self.gcloud = gcloud
        self.profile = PROFILES[profile]
        self.profile_name = profile

    def project(self, check_pool_grants=False):
        project = self.gcloud.run("projects", "describe", PROJECT)
        if (project.get("projectId") != PROJECT or str(project.get("projectNumber")) != PROJECT_NUMBER
                or project.get("lifecycleState") != "ACTIVE"):
            raise SetupError("Project identity/state mismatch; expected active flip-auto (941818435041)")
        if check_pool_grants:
            policy = self.gcloud.run("projects", "get-iam-policy", PROJECT)
            if any(f"/{self.profile.pool_resource}" in member for binding in policy.get("bindings", [])
                   for member in binding.get("members", [])):
                raise SetupError("Unexpected project-level transfer-pool grant; only secret-level grants are allowed")

    def pool(self, **kwargs):
        return self.gcloud.run("iam", "workload-identity-pools", "describe", self.profile.pool,
                               "--location=global", **kwargs)

    def provider(self, **kwargs):
        return self.gcloud.run("iam", "workload-identity-pools", "providers", "describe", PROVIDER,
                               f"--workload-identity-pool={self.profile.pool}", "--location=global", **kwargs)

    def validate_pool(self, pool):
        if (pool.get("name") != self.profile.pool_resource or pool.get("state") != "ACTIVE"
                or pool.get("disabled", False) or pool.get("mode", "FEDERATION_ONLY") != "FEDERATION_ONLY"
                or pool.get("description") != self.profile.description):
            raise SetupError("Existing pool differs or is disabled/deleted; refusing to alter or reuse it")

    def validate_provider(self, provider, expires_at):
        oidc = provider.get("oidc", {})
        if (provider.get("name") != self.profile.provider_resource or provider.get("state") != "ACTIVE"
                or provider.get("disabled", False)
                or provider.get("attributeMapping") != MAPPING
                or provider.get("attributeCondition") != ATTRIBUTE_CONDITION
                or provider.get("description") != DESCRIPTION_PREFIX + expires_at
                or oidc.get("issuerUri") != "https://token.actions.githubusercontent.com"
                or oidc.get("allowedAudiences", []) or oidc.get("jwksJson")
                or any(key in provider for key in ("aws", "saml", "x509"))):
            raise SetupError("Existing provider differs (including expiry) or is disabled/deleted; refusing to broaden trust")

    def validate_only_provider(self):
        providers = self.gcloud.run("iam", "workload-identity-pools", "providers", "list",
                                    f"--workload-identity-pool={self.profile.pool}", "--location=global", "--show-deleted")
        if any(provider.get("name") != self.profile.provider_resource for provider in providers):
            raise SetupError("Unexpected additional provider in transfer pool; direct WIF requires a dedicated pool")

    def policy(self, secret, **kwargs):
        return self.gcloud.run("secrets", "get-iam-policy", secret, **kwargs)

    def binding(self, operation, secret, condition, retry=False):
        # Preserve the entire condition when removing: IAM compares description too.
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", encoding="utf-8") as handle:
            json.dump(condition, handle)
            handle.flush()
            self.gcloud.run("secrets", operation, secret, f"--member={self.profile.member}", f"--role={ROLE}",
                            f"--condition-from-file={handle.name}", retry_not_found=retry)

    def check_or_apply(self, expires_at, apply=False):
        self.project(check_pool_grants=True)
        if apply:
            self.gcloud.run("services", "enable", *APIS)
        else:
            enabled = self.gcloud.run("services", "list", "--enabled")
            missing = set(APIS) - {service.get("config", {}).get("name") for service in enabled}
            if missing:
                print("Apply will enable APIs: " + ", ".join(sorted(missing)))
            if missing & {"iam.googleapis.com", "secretmanager.googleapis.com"}:
                raise SetupError("Read-only inspection requires IAM and Secret Manager APIs; --apply enables them")

        # Validate every preexisting resource before creating WIF or granting access.
        policies = {}
        for secret in self.profile.secrets:
            metadata = self.gcloud.run("secrets", "describe", secret)
            if metadata.get("name") != f"projects/{PROJECT_NUMBER}/secrets/{secret}":
                raise SetupError(f"Secret identity mismatch: {secret}")
            policy = policies[secret] = self.policy(secret)
            for binding in policy.get("bindings", []):
                members = binding.get("members", [])
                pool_members = [member for member in members if f"/{self.profile.pool_resource}/" in member]
                if pool_members and (pool_members != [self.profile.member] or binding.get("role") != ROLE
                                     or binding.get("condition") != expiry_condition(expires_at)):
                    raise SetupError(f"Unexpected transfer-pool permission on {secret}; refusing to extend access")

        pool = self.pool(missing_ok=True)
        provider = None
        if pool is not None:
            self.validate_pool(pool)
            self.validate_only_provider()
            provider = self.provider(missing_ok=True)
            if provider is not None:
                self.validate_provider(provider, expires_at)
        if not apply:
            print(f"Read-only check passed for {PROJECT} ({PROJECT_NUMBER}).")
            print(f"Pool: {'reuse' if pool else 'create'}; provider: {'reuse' if provider else 'create'}; expiry: {expires_at}")
            print(f"Apply grants only secretVersionAdder on {len(self.profile.secrets)} selected secrets. No values were read.")
            return

        created = pool is None or provider is None
        if pool is None:
            self.gcloud.run("iam", "workload-identity-pools", "create", self.profile.pool, "--location=global",
                            "--display-name=Flip Auto secret transfer", f"--description={self.profile.description}")
            self.validate_pool(self.pool(retry_not_found=True))
        if provider is None:
            self.gcloud.run("iam", "workload-identity-pools", "providers", "create-oidc", PROVIDER,
                            f"--workload-identity-pool={self.profile.pool}", "--location=global",
                            "--issuer-uri=https://token.actions.githubusercontent.com",
                            "--attribute-mapping=" + ",".join(f"{key}={value}" for key, value in MAPPING.items()),
                            f"--attribute-condition={ATTRIBUTE_CONDITION}",
                            f"--description={DESCRIPTION_PREFIX}{expires_at}", retry_not_found=created)
            self.validate_provider(self.provider(retry_not_found=True), expires_at)
        self.validate_only_provider()
        for secret, policy in policies.items():
            exists = any(binding.get("role") == ROLE and self.profile.member in binding.get("members", [])
                         and binding.get("condition") == expiry_condition(expires_at)
                         for binding in policy.get("bindings", []))
            if not exists:
                self.binding("add-iam-policy-binding", secret, expiry_condition(expires_at), retry=created)
        print(f"Temporary transfer access configured; expires at {expires_at}.")
        print(f"Provider: {self.profile.provider_resource}")
        print(f"Run --profile {self.profile_name} --revoke immediately after transfer. No secret values were read or workloads started.")

    def revoke(self):
        self.project()
        provider = self.provider(missing_ok=True)
        if provider is not None and provider.get("state") != "DELETED" and not provider.get("disabled", False):
            if provider.get("name") != self.profile.provider_resource:
                raise SetupError("Provider identity mismatch; refusing to disable a different resource")
            self.gcloud.run("iam", "workload-identity-pools", "providers", "update-oidc", PROVIDER,
                            f"--workload-identity-pool={self.profile.pool}", "--location=global", "--disabled")
        # Disabling stops fresh tokens; remove IAM grants too, including expired grants.
        for secret in self.profile.secrets:
            policy = self.policy(secret, missing_ok=True)
            if policy is None:
                continue
            for binding in policy.get("bindings", []):
                if (binding.get("role") == ROLE and self.profile.member in binding.get("members", [])
                        and owned_condition(binding.get("condition"))):
                    self.binding("remove-iam-policy-binding", secret, binding["condition"])
        print("Transfer provider disabled/absent; all matching temporary grants removed.")
        print("Existing production resources and secret versions were not changed.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--check", action="store_true", help="Read-only cloud preflight")
    actions.add_argument("--apply", action="store_true", help="Enable APIs and configure temporary direct WIF")
    actions.add_argument("--revoke", action="store_true", help="Disable provider and remove owned grants, including expired grants")
    parser.add_argument("--profile", choices=tuple(PROFILES), default="core", help="Fixed secret set; core preserves the original three-secret transfer")
    parser.add_argument("--expires-at", help="Required for check/apply; fixed UTC deadline within 48 hours")
    args = parser.parse_args(argv)
    try:
        setup = Setup(Gcloud(), profile=args.profile)
        if args.revoke:
            setup.revoke()
        else:
            setup.check_or_apply(validate_expiry(args.expires_at), apply=args.apply)
    except (SetupError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
