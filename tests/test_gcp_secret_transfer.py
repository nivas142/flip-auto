from __future__ import annotations

import base64
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import yaml


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("transfer_secrets", ROOT / "deploy/gcp/transfer-secrets.py")
transfer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(transfer)

SOURCES = {
    "EMAIL_USERNAME": "transfer-fixture-user@example.invalid",
    "EMAIL_APP_PASSWORD": " fixture-PASSWORD-é\nkeep-newline\n",
    "CLOUD_CMA_WEBHOOK_SECRET": "fixture-CMA-SECRET-6789",
}
DESTINATIONS = (
    "flip-auto-email-username",
    "flip-auto-email-app-password",
    "flip-auto-cma-webhook-secret",
)


def completed(secret_id, version=1, *, project="941818435041", stderr=b""):
    return subprocess.CompletedProcess(
        [], 0, f"projects/{project}/secrets/{secret_id}/versions/{version}\n".encode("ascii"), stderr,
    )


class SecretTransferTests(unittest.TestCase):
    def assertNoPayloads(self, value):
        rendered = str(value)
        for payload in SOURCES.values():
            self.assertNotIn(payload, rendered)
            self.assertNotIn(repr(payload.encode("utf-8")), rendered)
            self.assertNotIn(base64.b64encode(payload.encode("utf-8")).decode("ascii"), rendered)

    def test_missing_or_blank_source_prevents_every_write(self):
        for source in SOURCES:
            for empty in (None, "", " \n\t"):
                with self.subTest(source=source, empty=empty):
                    environ = {**SOURCES}
                    if empty is None:
                        environ.pop(source)
                    else:
                        environ[source] = empty
                    with patch.object(transfer.subprocess, "run") as run:
                        with self.assertRaises(transfer.TransferError) as caught:
                            transfer.transfer_secrets(environ, io.StringIO())
                    run.assert_not_called()
                    self.assertIn(source, str(caught.exception))
                    self.assertNoPayloads(caught.exception)
                    self.assertFalse(SOURCES.keys() & environ.keys())

    def test_all_payload_encodings_validated_before_writes(self):
        environ = {**SOURCES, "CLOUD_CMA_WEBHOOK_SECRET": "\udcff"}
        with patch.object(transfer.subprocess, "run") as run:
            with self.assertRaisesRegex(transfer.TransferError, "nothing was uploaded"):
                transfer.transfer_secrets(environ, io.StringIO())
        run.assert_not_called()

    def test_exact_destinations_and_bytes_only_in_stdin_with_safe_environment(self):
        environ = {
            **SOURCES,
            "PATH": "/usr/bin",
            "HOME": "/tmp/example-home",
            "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE": "/tmp/gha-creds-example.json",
            "GOOGLE_APPLICATION_CREDENTIALS": "/tmp/gha-creds-example.json",
            "CLOUDSDK_CORE_PROJECT": "attacker-project",
            "CLOUDSDK_CONFIG": "/tmp/unsafe-cloudsdk-config",
            "CLOUDSDK_CORE_LOG_HTTP": "true",
            "CLOUDSDK_CORE_VERBOSITY": "debug",
            "CLOUDSDK_CORE_DISABLE_FILE_LOGGING": "false",
            "CLOUDSDK_API_ENDPOINT_OVERRIDES_SECRETMANAGER": "https://attacker.invalid",
            "CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT": "attacker@example.invalid",
            "CLOUDSDK_CORE_TRACE_TOKEN": "debug-token",
            "PYTHONPATH": "/tmp/untrusted-python",
            "BASH_ENV": "/tmp/untrusted-shell",
        }
        output = io.StringIO()
        config_dirs = []

        def fake_run(command, **kwargs):
            self.assertFalse(SOURCES.keys() & environ.keys())
            self.assertFalse(SOURCES.keys() & kwargs["env"].keys())
            self.assertNoPayloads(command)
            self.assertNoPayloads(kwargs["env"])
            index = DESTINATIONS.index(command[4])
            self.assertEqual(command[:4], ["gcloud", "secrets", "versions", "add"])
            self.assertIn("--project=flip-auto", command)
            self.assertIn("--data-file=-", command)
            self.assertIn("--format=value(name)", command)
            self.assertIn("--no-log-http", command)
            self.assertIn("--verbosity=error", command)
            self.assertEqual(kwargs["input"], list(SOURCES.values())[index].encode("utf-8"))
            self.assertTrue(kwargs["capture_output"])
            self.assertFalse(kwargs["check"])
            self.assertEqual(kwargs["timeout"], 60)
            child_env = kwargs["env"]
            self.assertEqual(child_env["CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE"], "/tmp/gha-creds-example.json")
            self.assertEqual(child_env["CLOUDSDK_CORE_PROJECT"], "flip-auto")
            self.assertEqual(child_env["CLOUDSDK_CORE_LOG_HTTP"], "false")
            self.assertEqual(child_env["CLOUDSDK_CORE_VERBOSITY"], "error")
            self.assertEqual(child_env["CLOUDSDK_CORE_DISABLE_FILE_LOGGING"], "true")
            for unsafe in (
                "CLOUDSDK_API_ENDPOINT_OVERRIDES_SECRETMANAGER",
                "CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT", "CLOUDSDK_CORE_TRACE_TOKEN",
                "PYTHONPATH", "BASH_ENV",
            ):
                self.assertNotIn(unsafe, child_env)
            config_dir = Path(child_env["CLOUDSDK_CONFIG"])
            self.assertNotEqual(str(config_dir), "/tmp/unsafe-cloudsdk-config")
            self.assertTrue(config_dir.is_dir())
            config_dirs.append(config_dir)
            # Successful stderr is never forwarded, either.
            return completed(command[4], index + 1, stderr=SOURCES["EMAIL_USERNAME"].encode())

        with patch.object(transfer.subprocess, "run", side_effect=fake_run) as run:
            versions = transfer.transfer_secrets(environ, output)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(versions, tuple(
            f"projects/941818435041/secrets/{secret}/versions/{index + 1}"
            for index, secret in enumerate(DESTINATIONS)
        ))
        self.assertEqual(output.getvalue(), "\n".join(versions) + "\n")
        self.assertNoPayloads(output.getvalue())
        self.assertTrue(all(not path.exists() for path in config_dirs))

    def test_api_project_id_form_is_also_valid(self):
        results = [completed(secret, project="flip-auto") for secret in DESTINATIONS]
        with patch.object(transfer.subprocess, "run", side_effect=results):
            versions = transfer.transfer_secrets(dict(SOURCES), io.StringIO())
        self.assertEqual(len(versions), 3)

    def test_partial_failure_preserves_only_confirmed_ids_and_never_retries(self):
        leak = "|".join(SOURCES.values()).encode("utf-8")
        output = io.StringIO()
        config_dirs = []

        def fake_run(command, **kwargs):
            config_dirs.append(Path(kwargs["env"]["CLOUDSDK_CONFIG"]))
            if command[4] == DESTINATIONS[0]:
                return completed(DESTINATIONS[0], 42)
            return subprocess.CompletedProcess(command, 1, leak, leak)

        with patch.object(transfer.subprocess, "run", side_effect=fake_run) as run:
            with self.assertRaises(transfer.TransferError) as caught:
                transfer.transfer_secrets(dict(SOURCES), output)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(output.getvalue(), f"projects/941818435041/secrets/{DESTINATIONS[0]}/versions/42\n")
        self.assertIn(DESTINATIONS[1], str(caught.exception))
        self.assertIn("may have completed", str(caught.exception))
        self.assertNoPayloads(str(caught.exception) + output.getvalue())
        self.assertTrue(all(not path.exists() for path in config_dirs))

    def test_subprocess_exceptions_cannot_leak_values_or_cause_retry(self):
        leak = "|".join(SOURCES.values())
        failures = [
            OSError(leak),
            subprocess.TimeoutExpired([leak], 60, output=leak, stderr=leak),
            subprocess.CalledProcessError(1, [leak], output=leak, stderr=leak),
        ]
        for error in failures:
            with self.subTest(error=type(error).__name__):
                output = io.StringIO()
                with patch.object(transfer.subprocess, "run", side_effect=error) as run:
                    with self.assertRaises(transfer.TransferError) as caught:
                        transfer.transfer_secrets(dict(SOURCES), output)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(output.getvalue(), "")
                self.assertNoPayloads(caught.exception)
                self.assertTrue(caught.exception.__suppress_context__)

    def test_untrusted_success_output_cannot_be_logged(self):
        invalid_outputs = [
            b"projects/other-project/secrets/flip-auto-email-username/versions/1\n",
            b"projects/941818435041/secrets/other-secret/versions/1\n",
            b"projects/941818435041/secrets/flip-auto-email-username/versions/latest\n",
            b"projects/941818435041/secrets/flip-auto-email-username/versions/0\n",
            b"projects/941818435041/secrets/flip-auto-email-username/versions/01\n",
            b"projects/941818435041/secrets/flip-auto-email-username/versions/1\nextra\n",
            SOURCES["EMAIL_USERNAME"].encode(), b"\xff", b"",
        ]
        for raw in invalid_outputs:
            with self.subTest(raw=raw):
                output = io.StringIO()
                result = subprocess.CompletedProcess([], 0, raw, b"")
                with patch.object(transfer.subprocess, "run", return_value=result) as run:
                    with self.assertRaisesRegex(transfer.TransferError, "Unverified result") as caught:
                        transfer.transfer_secrets(dict(SOURCES), output)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(output.getvalue(), "")
                self.assertNoPayloads(caught.exception)

    def test_main_suppresses_unexpected_tracebacks(self):
        stderr = io.StringIO()
        with patch.object(sys, "argv", ["transfer-secrets.py"]), patch.object(sys, "stderr", stderr):
            with patch.object(transfer, "transfer_secrets", side_effect=RuntimeError(SOURCES["EMAIL_USERNAME"])):
                self.assertEqual(transfer.main(), 1)
        self.assertIn("Transfer stopped unexpectedly", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertNoPayloads(stderr.getvalue())

    def test_main_rejects_destination_arguments_without_echoing_them(self):
        stderr = io.StringIO()
        with patch.object(sys, "argv", ["transfer-secrets.py", SOURCES["EMAIL_USERNAME"]]):
            with patch.object(sys, "stderr", stderr), patch.object(transfer, "transfer_secrets") as run:
                self.assertEqual(transfer.main(), 2)
        run.assert_not_called()
        self.assertNoPayloads(stderr.getvalue())



class ZohoSecretTransferTests(unittest.TestCase):
    sources = {
        "ZOHO_EMAIL_USERNAME": "zoho-fixture@example.invalid",
        "ZOHO_EMAIL_APP_PASSWORD": " zoho-password-é\n",
    }
    destinations = ("flip-auto-zoho-email-username", "flip-auto-zoho-email-app-password")

    def test_only_zoho_bytes_are_uploaded_to_fixed_destinations(self):
        environ = {**SOURCES, **self.sources, "PATH": "/usr/bin"}
        output = io.StringIO()
        observed = []

        def fake_run(command, **kwargs):
            observed.append(command[4])
            source = next(name for name, dest in transfer.SECRET_PROFILES["zoho"] if dest == command[4])
            self.assertEqual(kwargs["input"], self.sources[source].encode())
            self.assertFalse((SOURCES.keys() | self.sources.keys()) & kwargs["env"].keys())
            return completed(command[4])

        with patch.object(transfer.subprocess, "run", side_effect=fake_run):
            versions = transfer.transfer_secrets(environ, output, profile="zoho")
        self.assertEqual(tuple(observed), self.destinations)
        self.assertEqual(len(versions), 2)
        self.assertFalse(self.sources.keys() & environ.keys())
        for value in (*SOURCES.values(), *self.sources.values()):
            self.assertNotIn(value, output.getvalue())

    def test_missing_either_zoho_secret_blocks_all_uploads(self):
        for source in self.sources:
            environ = dict(self.sources)
            environ[source] = " "
            with patch.object(transfer.subprocess, "run") as run:
                with self.assertRaisesRegex(transfer.TransferError, "Missing required"):
                    transfer.transfer_secrets(environ, io.StringIO(), profile="zoho")
            run.assert_not_called()
            self.assertFalse(self.sources.keys() & environ.keys())

    def test_unknown_profile_has_no_uploads(self):
        with patch.object(transfer.subprocess, "run") as run:
            with self.assertRaisesRegex(transfer.TransferError, "Unknown transfer profile"):
                transfer.transfer_secrets(dict(self.sources), io.StringIO(), profile="elsewhere")
        run.assert_not_called()

    def test_zoho_preflight_defaults_and_effective_inherited_window(self):
        for env in ({}, {"ZOHO_IMAP_HOST": "imap.zoho.com", "ZOHO_FOLDER": "Off-Market-Deals"},
                    {"EMAIL_LOOKBACK_HOURS": "48"}, {"ZOHO_LOOKBACK_HOURS": "48", "EMAIL_LOOKBACK_HOURS": "72"}):
            output = io.StringIO()
            with patch.object(transfer.subprocess, "run") as run:
                transfer.check_zoho_config(dict(env), output)
            run.assert_not_called()
            self.assertEqual(output.getvalue(), "ZOHO_HOST_MATCH=true\nZOHO_FOLDER_MATCH=true\nZOHO_LOOKBACK_MATCH=true\n")

    def test_zoho_preflight_mismatches_reveal_no_values_and_do_no_uploads(self):
        for setting, value, flag in (
            ("ZOHO_IMAP_HOST", "secret-host.invalid", "ZOHO_HOST_MATCH"),
            ("ZOHO_FOLDER", "private-folder", "ZOHO_FOLDER_MATCH"),
            ("ZOHO_LOOKBACK_HOURS", "72", "ZOHO_LOOKBACK_MATCH"),
            ("EMAIL_LOOKBACK_HOURS", "72", "ZOHO_LOOKBACK_MATCH"),
            ("ZOHO_LOOKBACK_HOURS", "private-invalid", "ZOHO_LOOKBACK_MATCH"),
        ):
            output = io.StringIO()
            environ = {setting: value}
            with patch.object(transfer.subprocess, "run") as run:
                with self.assertRaisesRegex(transfer.TransferError, "nothing was uploaded") as caught:
                    transfer.check_zoho_config(environ, output)
            run.assert_not_called()
            self.assertNotIn(setting, environ)
            self.assertIn(flag + "=false", output.getvalue())
            self.assertNotIn(value, output.getvalue() + str(caught.exception))

    def test_cli_zoho_profile_and_preflight_dispatch_are_fixed(self):
        for args, expected_profile in ((["--profile", "zoho"], "zoho"), (["--profile", "core"], "core")):
            with patch.object(sys, "argv", ["transfer-secrets.py", *args]):
                with patch.object(transfer, "transfer_secrets") as run:
                    self.assertEqual(transfer.main(), 0)
                    self.assertEqual(run.call_args.kwargs, {"profile": expected_profile})
        with patch.object(sys, "argv", ["transfer-secrets.py", "--check-zoho-config"]):
            with patch.object(transfer, "check_zoho_config") as check, patch.object(transfer, "transfer_secrets") as run:
                self.assertEqual(transfer.main(), 0)
                check.assert_called_once()
                run.assert_not_called()

class TransferWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # BaseLoader preserves the YAML 1.2 GitHub key "on" as text.
        cls.workflow = yaml.load(
            (ROOT / ".github/workflows/transfer-gcp-secrets.yml").read_text(), Loader=yaml.BaseLoader,
        )
        cls.job = cls.workflow["jobs"]["transfer"]

    def test_dispatch_main_identity_and_confirmation_gate(self):
        self.assertEqual(set(self.workflow["on"]), {"workflow_dispatch"})
        inputs = self.workflow["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(set(inputs), {"confirmation", "profile"})
        self.assertEqual(inputs["profile"]["default"], "core")
        self.assertEqual(inputs["profile"]["options"], ["core", "zoho"])
        self.assertEqual(inputs["confirmation"]["required"], "true")
        for guard in (
            "github.event_name == 'workflow_dispatch'", "github.ref == 'refs/heads/main'",
            "github.repository_id == '1172251948'", "github.repository_owner_id == '22221409'",
            "inputs.confirmation == 'COPY-THREE-SHADOW-SECRETS'",
        ):
            self.assertIn(guard, self.job["if"])
        self.assertEqual(self.job["environment"], "main")
        self.assertEqual(self.workflow["permissions"], {"contents": "read", "id-token": "write"})

    def test_pinned_actions_fixed_wif_and_step_scoped_secrets(self):
        steps = self.job["steps"]
        actions = {step["uses"].split("@")[0]: step for step in steps if "uses" in step}
        self.assertEqual(set(actions), {"actions/checkout", "google-github-actions/auth", "google-github-actions/setup-gcloud"})
        for step in actions.values():
            self.assertRegex(step["uses"], r"@[0-9a-f]{40}$")
        checkout = actions["actions/checkout"]["with"]
        self.assertEqual(checkout["ref"], "${{ github.sha }}")
        self.assertEqual(checkout["persist-credentials"], "false")
        auth = actions["google-github-actions/auth"]["with"]
        self.assertNotIn("service_account", auth)
        self.assertEqual(auth["project_id"], "flip-auto")
        self.assertEqual(auth["workload_identity_provider"], "projects/941818435041/locations/global/workloadIdentityPools/flip-auto-secret-transfer/providers/github")
        self.assertEqual(auth["cleanup_credentials"], "true")
        self.assertEqual(actions["google-github-actions/setup-gcloud"]["with"]["cache"], "false")
        source_steps = [step for step in steps if "secrets." in str(step)]
        self.assertEqual(len(source_steps), 1)
        self.assertEqual(source_steps[0]["run"], "python3 -I deploy/gcp/transfer-secrets.py")
        self.assertEqual(source_steps[0]["env"], {name: "${{ secrets." + name + " }}" for name in SOURCES})
        self.assertNotIn("secrets.", str(self.job["env"]))
        self.assertIn("RUNNER_DEBUG", steps[0]["run"])


    def test_zoho_job_checks_options_before_auth_and_injects_only_selected_secrets(self):
        job = self.workflow["jobs"]["transfer_zoho"]
        for guard in (
            "github.event_name == 'workflow_dispatch'", "github.ref == 'refs/heads/main'",
            "github.repository_id == '1172251948'", "github.repository_owner_id == '22221409'",
            "inputs.profile == 'zoho'", "inputs.confirmation == 'COPY-TWO-ZOHO-SECRETS'",
        ):
            self.assertIn(guard, job["if"])
        self.assertEqual(job["environment"], "main")
        steps = job["steps"]
        preflight = next(step for step in steps if step.get("run", "").endswith("--check-zoho-config"))
        auth = next(step for step in steps if step.get("uses", "").startswith("google-github-actions/auth@"))
        copy_step = next(step for step in steps if step.get("run", "").endswith("--profile zoho"))
        self.assertLess(steps.index(preflight), steps.index(auth))
        self.assertLess(steps.index(auth), steps.index(copy_step))
        self.assertEqual(set(preflight["env"]), {"ZOHO_IMAP_HOST", "ZOHO_FOLDER", "ZOHO_LOOKBACK_HOURS", "EMAIL_LOOKBACK_HOURS"})
        self.assertEqual(copy_step["env"], {name: "${{ secrets." + name + " }}" for name in ZohoSecretTransferTests.sources})
        self.assertEqual(auth["with"]["workload_identity_provider"], "projects/941818435041/locations/global/workloadIdentityPools/flip-auto-zoho-secret-transfer/providers/github")
        self.assertEqual(auth["with"]["cleanup_credentials"], "true")
        self.assertNotIn("service_account", auth["with"])
        for step in steps:
            if "uses" in step:
                self.assertRegex(step["uses"], r"@[0-9a-f]{40}$")
        for name in SOURCES:
            self.assertNotIn("secrets." + name, str(job))
        self.assertIn("RUNNER_DEBUG", steps[0]["run"])


if __name__ == "__main__":
    unittest.main()
