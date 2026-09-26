from __future__ import annotations

import copy
from contextlib import redirect_stdout
from datetime import datetime, timezone
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import unittest


SPEC = importlib.util.spec_from_file_location(
    "gcp_transfer_setup", Path(__file__).resolve().parents[1] / "deploy/gcp/setup-secret-transfer.py"
)
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)
EXPIRY = "2026-09-27T08:00:00Z"


class FakeGcloud:
    """Exercise actual CLI argument construction without cloud/network access."""
    def __init__(self, profile="core"):
        self.profile = setup.PROFILES[profile]
        self.calls = []
        self.conditions = []
        self.failures = {}
        self.project_number = setup.PROJECT_NUMBER
        self.project_policy = {"bindings": []}
        self.enabled_apis = list(setup.APIS)
        self.pool = None
        self.provider = None
        self.extra_providers = []
        self.policies = {secret: {"bindings": []} for secret in self.profile.secrets}

    def matching_resources(self):
        self.pool = {"name": self.profile.pool_resource, "state": "ACTIVE", "description": self.profile.description}
        self.provider = {
            "name": self.profile.provider_resource, "state": "ACTIVE", "attributeMapping": copy.deepcopy(setup.MAPPING),
            "attributeCondition": setup.ATTRIBUTE_CONDITION, "description": setup.DESCRIPTION_PREFIX + EXPIRY,
            "oidc": {"issuerUri": "https://token.actions.githubusercontent.com"},
        }

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        assert command[0] == "gcloud"
        assert "--project=flip-auto" in command
        assert kwargs["capture_output"] and kwargs["text"] and not kwargs["check"]
        args = command[1:]
        positional = [arg for arg in args if not arg.startswith("--")]
        key = tuple(positional)
        failures = self.failures.get(key, [])
        if failures:
            return subprocess.CompletedProcess(command, 1, "", failures.pop(0))
        flags = dict(arg[2:].split("=", 1) for arg in args if arg.startswith("--") and "=" in arg)
        result = None
        if positional[:2] == ["projects", "describe"]:
            result = {"projectId": "flip-auto", "projectNumber": self.project_number, "lifecycleState": "ACTIVE"}
        elif positional[:2] == ["projects", "get-iam-policy"]:
            result = self.project_policy
        elif positional[:2] == ["services", "list"]:
            result = [{"config": {"name": api}} for api in self.enabled_apis]
        elif positional[:2] == ["services", "enable"]:
            assert positional[2:] == list(setup.APIS)
        elif positional[:2] == ["secrets", "describe"]:
            result = {"name": f"projects/{self.project_number}/secrets/{positional[2]}"}
        elif positional[:2] == ["secrets", "get-iam-policy"]:
            result = self.policies[positional[2]]
        elif positional[:3] == ["iam", "workload-identity-pools", "describe"]:
            if self.pool is None:
                return subprocess.CompletedProcess(command, 1, "", "ERROR: NOT_FOUND: pool absent")
            result = self.pool
        elif positional[:3] == ["iam", "workload-identity-pools", "create"]:
            self.pool = {"name": self.profile.pool_resource, "state": "ACTIVE", "description": flags["description"]}
        elif positional[:4] == ["iam", "workload-identity-pools", "providers", "describe"]:
            if self.provider is None:
                return subprocess.CompletedProcess(command, 1, "", "ERROR: NOT_FOUND: provider absent")
            result = self.provider
        elif positional[:4] == ["iam", "workload-identity-pools", "providers", "list"]:
            result = ([self.provider] if self.provider else []) + self.extra_providers
        elif positional[:4] == ["iam", "workload-identity-pools", "providers", "create-oidc"]:
            self.provider = {
                "name": self.profile.provider_resource, "state": "ACTIVE", "description": flags["description"],
                "attributeMapping": dict(pair.split("=", 1) for pair in flags["attribute-mapping"].split(",")),
                "attributeCondition": flags["attribute-condition"], "oidc": {"issuerUri": flags["issuer-uri"]},
            }
        elif positional[:4] == ["iam", "workload-identity-pools", "providers", "update-oidc"]:
            assert "--disabled" in args
            self.provider["disabled"] = True
        elif positional[0] == "secrets" and positional[1] in ("add-iam-policy-binding", "remove-iam-policy-binding"):
            assert flags["member"] == self.profile.member and flags["role"] == setup.ROLE
            condition = json.loads(Path(flags["condition-from-file"]).read_text())
            self.conditions.append(condition)
            policy = self.policies[positional[2]]["bindings"]
            if positional[1] == "add-iam-policy-binding":
                policy.append({"role": flags["role"], "members": [flags["member"]], "condition": condition})
            else:
                for binding in policy[:]:
                    if (binding["role"] == flags["role"] and binding.get("condition") == condition
                            and flags["member"] in binding["members"]):
                        binding["members"].remove(flags["member"])
                        if not binding["members"]:
                            policy.remove(binding)
        else:
            raise AssertionError(f"Unexpected gcloud operation: {command}")
        return subprocess.CompletedProcess(command, 0, json.dumps(result), "")

    def mutations(self):
        return [call for call in self.calls if any(word in call for word in (
            "enable", "create", "create-oidc", "update-oidc", "add-iam-policy-binding", "remove-iam-policy-binding"
        ))]


class TransferSetupTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGcloud()
        self.sleeps = []
        self.client = setup.Gcloud(self.fake, self.sleeps.append)
        self.subject = setup.Setup(self.client)
        self.output = io.StringIO()

    def run_setup(self, apply=False):
        with redirect_stdout(self.output):
            self.subject.check_or_apply(EXPIRY, apply=apply)

    def test_check_is_read_only_and_missing_pool_is_valid_preflight(self):
        self.run_setup()
        self.assertEqual(self.fake.mutations(), [])
        self.assertIn("Pool: create; provider: create", self.output.getvalue())
        self.assertEqual(self.sleeps, [])

    def test_apply_scope_and_rerun_are_fixed(self):
        self.run_setup(apply=True)
        mutations = self.fake.mutations()
        self.assertEqual(len(mutations), 6)  # API enable, pool, provider, three grants.
        self.assertEqual(self.fake.provider["attributeMapping"]["google.subject"], "assertion.sub")
        self.assertEqual(self.fake.provider["attributeMapping"]["attribute.repository_owner_id"], "assertion.repository_owner_id")
        for claim, expected in {
            "repository_owner_id": "22221409", "repository_id": "1172251948", "repository": "nivas142/flip-auto",
            "ref": "refs/heads/main", "event_name": "workflow_dispatch",
            "workflow_ref": "nivas142/flip-auto/.github/workflows/transfer-gcp-secrets.yml@refs/heads/main",
            "sub": "repo:nivas142/flip-auto:environment:main",
        }.items():
            self.assertIn(f"assertion.{claim} == '{expected}'", self.fake.provider["attributeCondition"])
        self.assertEqual(self.fake.conditions, [setup.expiry_condition(EXPIRY)] * 3)
        grants = [call for call in mutations if "add-iam-policy-binding" in call]
        self.assertEqual([call[3] for call in grants], list(setup.SECRETS))
        self.assertTrue(all(f"--role={setup.ROLE}" in call and f"--member={setup.MEMBER}" in call for call in grants))
        self.fake.calls.clear()
        self.run_setup(apply=True)
        self.assertEqual(len(self.fake.mutations()), 1)  # Idempotent API enable only.

    def test_wrong_project_fails_before_any_mutation(self):
        self.fake.project_number = "123456789"
        with self.assertRaisesRegex(setup.SetupError, "Project identity"):
            self.run_setup(apply=True)
        self.assertEqual(self.fake.mutations(), [])

    def test_project_grants_fail_before_api_enablement(self):
        for member in (setup.MEMBER, f"principalSet://iam.googleapis.com/{setup.POOL_RESOURCE}/*"):
            self.fake.project_policy = {"bindings": [{"role": "roles/editor", "members": [member]}]}
            with self.subTest(member=member), self.assertRaisesRegex(setup.SetupError, "project-level"):
                self.run_setup(apply=True)
        self.assertEqual(self.fake.mutations(), [])

    def test_check_reports_missing_sts_without_enabling_it(self):
        self.fake.enabled_apis.remove("sts.googleapis.com")
        self.run_setup()
        self.assertIn("Apply will enable APIs: sts.googleapis.com", self.output.getvalue())
        self.assertEqual(self.fake.mutations(), [])

    def test_changed_provider_or_extra_provider_never_grants(self):
        changes = [
            lambda p: p.update(attributeCondition="true"),
            lambda p: p["attributeMapping"].update({"google.subject": "assertion.repository"}),
            lambda p: p.update(description=setup.DESCRIPTION_PREFIX + "2026-09-28T08:00:00Z"),
            lambda p: p.update(disabled=True),
            lambda p: p["oidc"].update(allowedAudiences=["unrelated-audience"]),
            lambda p: p["oidc"].update(jwksJson='{"keys":[]}'),
        ]
        for change in changes:
            with self.subTest(change=change):
                self.fake.matching_resources()
                change(self.fake.provider)
                with self.assertRaisesRegex(setup.SetupError, "Existing provider differs"):
                    self.run_setup(apply=True)
                self.assertFalse(any("add-iam-policy-binding" in call for call in self.fake.calls))
        self.fake.matching_resources()
        self.fake.extra_providers = [{"name": setup.POOL_RESOURCE + "/providers/another"}]
        with self.assertRaisesRegex(setup.SetupError, "additional provider"):
            self.run_setup(apply=True)

    def test_existing_broader_or_different_expiry_grant_is_rejected(self):
        for condition in (None, setup.expiry_condition("2026-09-28T08:00:00Z")):
            self.fake.policies[setup.SECRETS[0]]["bindings"] = [
                {"members": [setup.MEMBER], "role": setup.ROLE, "condition": condition}
            ]
            with self.subTest(condition=condition), self.assertRaisesRegex(setup.SetupError, "Unexpected transfer-pool"):
                self.run_setup(apply=True)
        self.assertIsNone(self.fake.pool)

    def test_permission_error_is_not_treated_as_absence_or_retried(self):
        key = ("iam", "workload-identity-pools", "describe", setup.POOL)
        self.fake.failures[key] = ["ERROR: PERMISSION_DENIED: access denied"]
        with self.assertRaisesRegex(setup.GcloudError, "PERMISSION_DENIED"):
            self.run_setup(apply=True)
        self.assertIsNone(self.fake.pool)
        self.assertEqual(self.sleeps, [])

    def test_create_to_grant_propagation_retries_only_not_found(self):
        key = ("secrets", "add-iam-policy-binding", setup.SECRETS[0])
        self.fake.failures[key] = ["ERROR: NOT_FOUND: pool propagation pending"] * 2
        self.run_setup(apply=True)
        self.assertEqual(self.sleeps, [1, 2])
        self.assertEqual(len(self.fake.conditions), 3)

    def test_retry_is_bounded_and_permission_failure_is_immediate(self):
        for message, expected_sleeps in (("ERROR: NOT_FOUND: pending", list(setup.RETRY_DELAYS)),
                                         ("ERROR: PERMISSION_DENIED: denied", [])):
            fake = FakeGcloud()
            fake.failures[("secrets", "add-iam-policy-binding", setup.SECRETS[0])] = [message] * 20
            sleeps = []
            with self.subTest(message=message), self.assertRaises(setup.GcloudError):
                setup.Setup(setup.Gcloud(fake, sleeps.append)).check_or_apply(EXPIRY, apply=True)
            self.assertEqual(sleeps, expected_sleeps)

    def test_revoke_removes_expired_bindings_preserves_others_and_is_idempotent(self):
        self.fake.matching_resources()
        expired = setup.expiry_condition("2020-01-01T00:00:00Z")
        expired["description"] = "Preserve exact condition when removing"
        runtime = "serviceAccount:flip-auto-shadow@flip-auto.iam.gserviceaccount.com"
        for secret in setup.SECRETS:
            self.fake.policies[secret]["bindings"] = [
                {"members": [setup.MEMBER, "user:owner@example.com"], "role": setup.ROLE, "condition": expired},
                {"members": [runtime], "role": "roles/secretmanager.secretAccessor"},
            ]
        with redirect_stdout(self.output):
            self.subject.revoke()
        self.assertTrue(self.fake.provider["disabled"])
        self.assertEqual(self.fake.conditions, [expired] * 3)
        for policy in self.fake.policies.values():
            self.assertEqual(policy["bindings"][0]["members"], ["user:owner@example.com"])
            self.assertEqual(policy["bindings"][1]["members"], [runtime])
        self.fake.calls.clear()
        with redirect_stdout(self.output):
            self.subject.revoke()
        self.assertEqual(self.fake.mutations(), [])

    def test_revoke_missing_provider_is_safe(self):
        with redirect_stdout(self.output):
            self.subject.revoke()
        self.assertEqual(self.fake.mutations(), [])

    def test_expiry_requires_explicit_future_utc_with_48_hour_limit(self):
        now = datetime(2026, 9, 26, 8, tzinfo=timezone.utc)
        self.assertEqual(setup.validate_expiry(EXPIRY, now), EXPIRY)
        self.assertEqual(setup.validate_expiry("2026-09-28T08:00:00Z", now), "2026-09-28T08:00:00Z")
        for invalid in (None, "tomorrow", "2026-09-27T08:00:00+00:00", "2026-09-31T08:00:00Z",
                        "2026-09-26T08:00:00Z", "2026-09-28T08:00:01Z", "2025-09-27T08:00:00Z"):
            with self.subTest(invalid=invalid), self.assertRaises(setup.SetupError):
                setup.validate_expiry(invalid, now)


class ZohoTransferSetupTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGcloud("zoho")
        self.profile = setup.PROFILES["zoho"]
        self.subject = setup.Setup(setup.Gcloud(self.fake, lambda _: None), profile="zoho")
        self.output = io.StringIO()

    def test_zoho_apply_grants_only_two_selected_secrets_with_separate_identity(self):
        with redirect_stdout(self.output):
            self.subject.check_or_apply(EXPIRY, apply=True)
        grants = [call for call in self.fake.calls if "add-iam-policy-binding" in call]
        self.assertEqual([call[3] for call in grants], [
            "flip-auto-zoho-email-username", "flip-auto-zoho-email-app-password",
        ])
        self.assertTrue(all(f"--member={self.profile.member}" in call for call in grants))
        self.assertEqual(self.fake.conditions, [setup.expiry_condition(EXPIRY)] * 2)
        self.assertEqual(self.fake.provider["name"], self.profile.provider_resource)
        self.assertEqual(self.fake.provider["attributeCondition"], setup.ATTRIBUTE_CONDITION)
        self.assertIn("--profile zoho --revoke", self.output.getvalue())
        for call in self.fake.calls:
            self.assertNotIn(setup.POOL, call)
            self.assertNotIn(f"--workload-identity-pool={setup.POOL}", call)
            self.assertFalse(setup.SECRETS and set(setup.SECRETS) & set(call))
        self.fake.calls.clear()
        with redirect_stdout(self.output):
            self.subject.check_or_apply(EXPIRY, apply=True)
        self.assertEqual(len(self.fake.mutations()), 1)  # API enable only.

    def test_zoho_revoke_preserves_runtime_and_unselected_identity_grants(self):
        self.fake.matching_resources()
        expired = setup.expiry_condition("2020-01-01T00:00:00Z")
        runtime_binding = {"role": "roles/secretmanager.secretAccessor", "members": [
            "serviceAccount:flip-auto-shadow@flip-auto.iam.gserviceaccount.com",
        ]}
        unselected_binding = {"role": setup.ROLE, "members": [setup.MEMBER], "condition": expired}
        for secret in self.profile.secrets:
            self.fake.policies[secret]["bindings"] = [
                {"role": setup.ROLE, "members": [self.profile.member], "condition": expired},
                copy.deepcopy(runtime_binding), copy.deepcopy(unselected_binding),
            ]
        with redirect_stdout(self.output):
            self.subject.revoke()
        self.assertTrue(self.fake.provider["disabled"])
        for policy in self.fake.policies.values():
            self.assertEqual(policy["bindings"], [runtime_binding, unselected_binding])
        self.assertEqual(len([call for call in self.fake.calls if "remove-iam-policy-binding" in call]), 2)
        self.fake.calls.clear()
        with redirect_stdout(self.output):
            self.subject.revoke()
        self.assertEqual(self.fake.mutations(), [])

    def test_disabled_zoho_provider_is_never_reactivated(self):
        self.fake.matching_resources()
        self.fake.provider["disabled"] = True
        with self.assertRaisesRegex(setup.SetupError, "Existing provider differs"):
            self.subject.check_or_apply(EXPIRY, apply=True)
        self.assertFalse(any("update-oidc" in call or "add-iam-policy-binding" in call for call in self.fake.calls))

    def test_zoho_project_scope_grant_is_rejected(self):
        self.fake.project_policy = {"bindings": [{"role": "roles/editor", "members": [self.profile.member]}]}
        with self.assertRaisesRegex(setup.SetupError, "project-level"):
            self.subject.check_or_apply(EXPIRY, apply=True)
        self.assertEqual(self.fake.mutations(), [])

    def test_unknown_profile_makes_no_cloud_calls(self):
        with self.assertRaisesRegex(setup.SetupError, "Unknown transfer profile"):
            setup.Setup(setup.Gcloud(self.fake), profile="arbitrary")
        self.assertEqual(self.fake.calls, [])


if __name__ == "__main__":
    unittest.main()
