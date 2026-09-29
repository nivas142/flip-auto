from __future__ import annotations

from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


transfer = load_module("production_transfer", ROOT / "deploy/gcp/transfer-secrets.py")
setup_tests = load_module("production_transfer_setup_fakes", ROOT / "tests/test_gcp_transfer_setup.py")
setup = setup_tests.setup
AUTH = {
    "CLOUD_CMA_API_KEY": " fixture-api-key-é\n",
    "TELEGRAM_BOT_TOKEN": "fixture-bot-token-1234",
    "TELEGRAM_CHAT_ID": "-123456789",
}
SETTINGS = {
    "ZOHO_IMAP_HOST": " imappro.zoho.com ",
    "GSHEET_PUBLIC_URL": "https://docs.google.com/spreadsheets/d/fixture-sheet-id/edit?gid=9#gid=9",
}
DESTINATIONS = (
    "flip-auto-cloud-cma-api-key", "flip-auto-telegram-bot-token",
    "flip-auto-telegram-chat-id", "flip-auto-live-settings",
)


def completed(secret, version=1):
    return subprocess.CompletedProcess([], 0, f"projects/941818435041/secrets/{secret}/versions/{version}\n".encode(), b"")


class ProductionTransferTests(unittest.TestCase):
    def test_exactly_four_validated_payloads_use_stdin_and_no_values_are_logged(self):
        environ = {
            **AUTH, **SETTINGS, "PATH": os.defpath,
            "FLIP_AUTO_LIVE_SETTINGS_JSON": '{"injected":true}',
            "GSHEET_SERVICE_ACCOUNT_JSON": '{"private_key":"DO-NOT-TRANSFER"}',
            "CLOUDSDK_API_ENDPOINT_OVERRIDES_SECRETMANAGER": "https://untrusted.invalid",
        }
        output = io.StringIO()
        observed = []

        def fake_run(command, **kwargs):
            observed.append(command[4])
            self.assertEqual(command[:4], ["gcloud", "secrets", "versions", "add"])
            self.assertIn("--data-file=-", command)
            self.assertIn("--project=flip-auto", command)
            self.assertNotIn("GSHEET_SERVICE_ACCOUNT_JSON", kwargs["env"])
            self.assertNotIn("CLOUDSDK_API_ENDPOINT_OVERRIDES_SECRETMANAGER", kwargs["env"])
            self.assertFalse(set(AUTH) & environ.keys())
            self.assertFalse(set(transfer.PRODUCTION_SETTING_SOURCES) & environ.keys())
            self.assertFalse(set(AUTH) & kwargs["env"].keys())
            if command[4] == DESTINATIONS[-1]:
                self.assertEqual(json.loads(kwargs["input"]), {
                    "schema_version": 1, "email_lookback_hours": 48,
                    "zoho_imap_host": "imappro.zoho.com", "zoho_folder": "Off-Market-Deals",
                    "zoho_lookback_hours": 48, "gsheet_public_csv_url": "",
                    "gsheet_public_url": SETTINGS["GSHEET_PUBLIC_URL"],
                    "gsheet_spreadsheet_id": "fixture-sheet-id",
                })
            else:
                source = next(name for name, destination in transfer.SECRET_PROFILES["production"] if destination == command[4])
                self.assertEqual(kwargs["input"], AUTH[source].encode())
            return completed(command[4])

        with patch.object(transfer.subprocess, "run", side_effect=fake_run) as run:
            versions = transfer.transfer_secrets(environ, output, profile="production")
        self.assertEqual(run.call_count, 4)
        self.assertEqual(tuple(observed), DESTINATIONS)
        self.assertEqual(output.getvalue(), "\n".join(versions) + "\n")
        for value in (*AUTH.values(), *SETTINGS.values(), "DO-NOT-TRANSFER", "injected"):
            self.assertNotIn(value, output.getvalue())

    def test_missing_auth_invalid_settings_and_bad_encoding_block_all_writes(self):
        invalid_cases = [{name: " \n"} for name in AUTH]
        invalid_cases += [
            {"CLOUD_CMA_API_KEY": "\udcff"}, {"ZOHO_IMAP_HOST": ""},
            {"ZOHO_IMAP_HOST": "imap.zoho.com"}, {"EMAIL_LOOKBACK_HOURS": "0"},
            {"EMAIL_LOOKBACK_HOURS": "169"}, {"ZOHO_LOOKBACK_HOURS": "nonnumeric-private-value"},
            {"ZOHO_FOLDER": "\udcff"}, {"GSHEET_PUBLIC_URL": ""},
            {"ZOHO_FOLDER": "private-folder\x00"}, {"ZOHO_FOLDER": "REPLACE_WITH_FOLDER"},
            {"GSHEET_SPREADSHEET_ID": "private/invalid-id"}, {"GSHEET_SPREADSHEET_ID": "REPLACE_WITH_SHEET_ID"},
            {"GSHEET_PUBLIC_URL": "http://private.invalid/sheet"},
            {"GSHEET_PUBLIC_URL": "https://user:password@private.invalid/sheet"},
            {"GSHEET_PUBLIC_URL": "https://private.invalid:badport/sheet"},
            {"GSHEET_PUBLIC_URL": "https://docs.google.com/spreadsheets/d/REPLACE_WITH_SHEET_ID/edit"},
            {"GSHEET_PUBLIC_URL": "https://private.invalid/no-sheet-id"},
        ]
        for overrides in invalid_cases:
            with self.subTest(overrides=overrides):
                environ = {**AUTH, **SETTINGS, **overrides}
                output = io.StringIO()
                with patch.object(transfer.subprocess, "run") as run:
                    with self.assertRaises(transfer.TransferError) as caught:
                        transfer.transfer_secrets(environ, output, profile="production")
                run.assert_not_called()
                self.assertEqual(output.getvalue(), "")
                self.assertFalse(set(AUTH) & environ.keys())
                self.assertFalse(set(transfer.PRODUCTION_SETTING_SOURCES) & environ.keys())
                for value in (*AUTH.values(), "nonnumeric-private-value", "private.invalid"):
                    self.assertNotIn(value, str(caught.exception))

    def test_public_csv_priority_and_effective_lookback_inheritance(self):
        settings = transfer._production_settings({
            **SETTINGS, "GSHEET_PUBLIC_CSV_URL": " https://docs.google.com/spreadsheets/d/fixture/export?format=csv ",
            "GSHEET_PUBLIC_URL": "ignored-invalid-lower-priority-source",
            "GSHEET_SPREADSHEET_ID": " explicit-id ", "EMAIL_LOOKBACK_HOURS": "72",
        })
        self.assertEqual(settings["email_lookback_hours"], 72)
        self.assertEqual(settings["zoho_lookback_hours"], 72)
        self.assertEqual(settings["gsheet_public_url"], "")
        self.assertEqual(settings["gsheet_spreadsheet_id"], "explicit-id")
        self.assertEqual(settings["gsheet_public_csv_url"], "https://docs.google.com/spreadsheets/d/fixture/export?format=csv")
        settings = transfer._production_settings({**SETTINGS, "EMAIL_LOOKBACK_HOURS": "72", "ZOHO_LOOKBACK_HOURS": "24"})
        self.assertEqual(settings["zoho_lookback_hours"], 24)

    def test_private_sheet_only_is_explicitly_rejected_before_auth_or_write(self):
        output = io.StringIO()
        environ = {"ZOHO_IMAP_HOST": "imappro.zoho.com", "GSHEET_SERVICE_ACCOUNT_JSON": "secret-private-key"}
        with patch.object(transfer.subprocess, "run") as run:
            with self.assertRaisesRegex(transfer.TransferError, "private-sheet service-account migration is unsupported") as caught:
                transfer.check_production_config(environ, output)
        run.assert_not_called()
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn("secret-private-key", str(caught.exception))

    def test_preflight_logs_only_fixed_flags_and_cli_dispatch_is_explicit(self):
        output = io.StringIO()
        with patch.object(transfer.subprocess, "run") as run:
            transfer.check_production_config(dict(SETTINGS), output)
        run.assert_not_called()
        self.assertEqual(output.getvalue(), "PRODUCTION_SETTINGS_VALID=true\nPRODUCTION_PUBLIC_SHEET=true\n")
        with patch.object(transfer.sys, "argv", ["transfer-secrets.py", "--check-production-config"]):
            with patch.object(transfer, "check_production_config") as check, patch.object(transfer, "transfer_secrets") as run:
                self.assertEqual(transfer.main(), 0)
                check.assert_called_once()
                run.assert_not_called()
        with patch.object(transfer.sys, "argv", ["transfer-secrets.py", "--profile", "production"]):
            with patch.object(transfer, "transfer_secrets") as run:
                self.assertEqual(transfer.main(), 0)
                self.assertEqual(run.call_args.kwargs, {"profile": "production"})

    def test_partial_failure_has_no_retry_or_payload_log(self):
        output = io.StringIO()
        results = [completed(DESTINATIONS[0], 7), subprocess.CompletedProcess([], 1, b"leaked-remote-data", b"leaked-remote-data")]
        with patch.object(transfer.subprocess, "run", side_effect=results) as run:
            with self.assertRaisesRegex(transfer.TransferError, "may have completed") as caught:
                transfer.transfer_secrets({**AUTH, **SETTINGS}, output, profile="production")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(output.getvalue(), f"projects/941818435041/secrets/{DESTINATIONS[0]}/versions/7\n")
        self.assertNotIn("leaked-remote-data", output.getvalue() + str(caught.exception))


class ProductionSetupTests(unittest.TestCase):
    def setUp(self):
        self.fake = setup_tests.FakeGcloud("production")
        self.subject = setup.Setup(setup.Gcloud(self.fake, lambda _: None), profile="production")
        self.profile = self.subject.profile

    def apply(self):
        with redirect_stdout(io.StringIO()):
            self.subject.check_or_apply(setup_tests.EXPIRY, apply=True)

    def test_production_pins_distinct_workflow_and_grants_only_four_secret_adders(self):
        for profile in setup.PROFILES.values():
            self.assertRegex(profile.pool, r"^[a-z0-9][a-z0-9-]{2,30}[a-z0-9]$")
            self.assertFalse(profile.pool.startswith("gcp-"))
        self.apply()
        grants = [call for call in self.fake.calls if "add-iam-policy-binding" in call]
        self.assertEqual([call[3] for call in grants], list(DESTINATIONS))
        self.assertTrue(all(f"--role={setup.ROLE}" in call for call in grants))
        self.assertTrue(all(f"--member={self.profile.member}" in call for call in grants))
        self.assertEqual(self.fake.provider["attributeCondition"], self.profile.attribute_condition)
        self.assertIn("transfer-gcp-production-secrets.yml@refs/heads/main", self.fake.provider["attributeCondition"])
        self.assertIn("assertion.repository_id == '1172251948'", self.fake.provider["attributeCondition"])
        self.assertIn("assertion.repository_owner_id == '22221409'", self.fake.provider["attributeCondition"])
        self.assertIn("assertion.sub == 'repo:nivas142/flip-auto:environment:main'", self.fake.provider["attributeCondition"])
        self.assertIn("assertion.event_name == 'workflow_dispatch'", self.fake.provider["attributeCondition"])
        self.assertEqual(self.fake.conditions, [setup.expiry_condition(setup_tests.EXPIRY)] * 4)
        for profile in ("core", "zoho"):
            self.assertEqual(setup.PROFILES[profile].attribute_condition, setup.ATTRIBUTE_CONDITION)
            for call in self.fake.calls:
                self.assertNotIn(f"--workload-identity-pool={setup.PROFILES[profile].pool}", call)
        self.fake.calls.clear()
        self.apply()
        self.assertEqual(len(self.fake.mutations()), 1)  # Idempotent API enable only.

    def test_existing_other_workflow_trust_and_disabled_provider_are_rejected(self):
        self.apply()
        for update in ({"attributeCondition": setup.ATTRIBUTE_CONDITION}, {"disabled": True}):
            self.fake.provider["attributeCondition"] = self.profile.attribute_condition
            self.fake.provider["disabled"] = False
            self.fake.provider.update(update)
            self.fake.calls.clear()
            with self.subTest(update=update), self.assertRaisesRegex(setup.SetupError, "Existing provider differs"):
                self.apply()
            self.assertFalse(any("update-oidc" in call or "add-iam-policy-binding" in call for call in self.fake.calls))

    def test_revoke_disables_only_selected_provider_and_retains_runtime_access(self):
        self.apply()
        runtime = {"role": "roles/secretmanager.secretAccessor", "members": ["serviceAccount:flip-auto-live@flip-auto.iam.gserviceaccount.com"]}
        for policy in self.fake.policies.values():
            policy["bindings"].append(dict(runtime))
        self.fake.calls.clear()
        with redirect_stdout(io.StringIO()):
            self.subject.revoke()
        self.assertTrue(self.fake.provider["disabled"])
        self.assertEqual([call[3] for call in self.fake.calls if "remove-iam-policy-binding" in call], list(DESTINATIONS))
        self.assertTrue(all(policy["bindings"] == [runtime] for policy in self.fake.policies.values()))
        self.fake.calls.clear()
        with redirect_stdout(io.StringIO()):
            self.subject.revoke()
        self.assertEqual(self.fake.mutations(), [])


class ProductionWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = yaml.load((ROOT / ".github/workflows/transfer-gcp-production-secrets.yml").read_text(), Loader=yaml.BaseLoader)
        cls.job = cls.workflow["jobs"]["transfer_production"]

    def test_workflow_uses_pinned_provider_and_checks_before_authentication(self):
        self.assertEqual(set(self.workflow["on"]), {"workflow_dispatch"})
        self.assertEqual(set(self.workflow["on"]["workflow_dispatch"]["inputs"]), {"confirmation"})
        self.assertEqual(self.job["environment"], "main")
        self.assertEqual(self.workflow["permissions"], {"contents": "read", "id-token": "write"})
        for guard in ("github.ref == 'refs/heads/main'", "github.event_name == 'workflow_dispatch'",
                      "github.repository_id == '1172251948'", "github.repository_owner_id == '22221409'"):
            self.assertIn(guard, self.job["if"])
        steps = self.job["steps"]
        actions = {step["uses"].split("@")[0]: step for step in steps if "uses" in step}
        self.assertEqual(set(actions), {"actions/checkout", "google-github-actions/setup-gcloud", "google-github-actions/auth"})
        for step in actions.values():
            self.assertRegex(step["uses"], r"@[0-9a-f]{40}$")
        auth = actions["google-github-actions/auth"]
        self.assertEqual(auth["with"]["workload_identity_provider"], setup.PROFILES["production"].provider_resource)
        self.assertNotIn("service_account", auth["with"])
        self.assertEqual(auth["with"]["cleanup_credentials"], "true")
        self.assertEqual(actions["actions/checkout"]["with"], {"ref": "${{ github.sha }}", "persist-credentials": "false"})
        self.assertEqual(actions["google-github-actions/setup-gcloud"]["with"]["cache"], "false")
        preflight = next(step for step in steps if step.get("run", "").endswith("--check-production-config"))
        self.assertLess(steps.index(preflight), steps.index(auth))
        self.assertIn("RUNNER_DEBUG", steps[1]["run"])
        self.assertEqual(set(preflight["env"]), set(transfer.PRODUCTION_SETTING_SOURCES))
        copy_step = steps[-1]
        self.assertEqual(copy_step["run"], "python3 -I deploy/gcp/transfer-secrets.py --profile production")
        self.assertEqual(set(copy_step["env"]), set(AUTH) | set(transfer.PRODUCTION_SETTING_SOURCES))
        for name, value in copy_step["env"].items():
            self.assertEqual(value, "${{ secrets." + name + " }}")
        self.assertNotIn("secrets.", str(self.job["env"]))
        self.assertNotIn("GSHEET_SERVICE_ACCOUNT_JSON", str(self.workflow))
        self.assertFalse(any("continue-on-error" in step for step in steps))

    def test_confirmation_and_debug_guards_fail_without_executing_inputs(self):
        steps = self.job["steps"]
        self.assertEqual(steps[0]["env"], {"TRANSFER_CONFIRMATION": "${{ inputs.confirmation }}"})
        script = steps[0]["run"] + steps[1]["run"] + "\nprintf '%s\\n' PASSED\n"
        with tempfile.TemporaryDirectory() as directory:
            for confirmation, debug, valid in (
                ("COPY-PRODUCTION-SECRETS", "", True), ("COPY-PRODUCTION-SECRETS", "1", False),
                ("COPY-TWO-ZOHO-SECRETS", "", False), (" COPY-PRODUCTION-SECRETS ", "", False),
                ("$(touch INJECTED)`touch INJECTED`", "", False),
            ):
                result = subprocess.run(["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
                                        cwd=directory, env={"PATH": os.defpath, "TRANSFER_CONFIRMATION": confirmation, "RUNNER_DEBUG": debug},
                                        capture_output=True, text=True, timeout=5, check=False)
                self.assertEqual(result.returncode, 0 if valid else 1)
                self.assertEqual(result.stdout, "PASSED\n" if valid else "")
                self.assertFalse((Path(directory) / "INJECTED").exists())


if __name__ == "__main__":
    unittest.main()
