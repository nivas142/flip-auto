"""Exercise setup ordering/fail-closed recovery without contacting GCP."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "deploy/gcp/create-paused-scheduler.sh"
ACCOUNT = "flip-auto-shadow-scheduler@flip-auto.iam.gserviceaccount.com"
MARKER = "Managed by flip-auto/create-paused-scheduler.sh"
FAKE_GCLOUD = r'''
import json, os, pathlib, sys
path = pathlib.Path(os.environ['SCENARIO_PATH'])
s = json.loads(path.read_text())
a = sys.argv[1:]
s.setdefault('calls', []).append(a)
result, error = {}, None
if a[:3] == ['iam', 'service-accounts', 'create']:
    if s.get('account_exists'):
        error = 'ALREADY_EXISTS: service account exists'
    else:
        s['account_exists'] = True
        result = s['account']
elif a[:3] == ['iam', 'service-accounts', 'describe']:
    result = s['account']
elif a[:2] == ['projects', 'get-iam-policy']:
    result = s.get('project_policy', {})
elif a[:3] == ['run', 'jobs', 'get-iam-policy']:
    result = s.get('job_policy', {})
elif a[:3] == ['scheduler', 'jobs', 'list']:
    result = s.get('jobs', [])
elif a[:3] == ['scheduler', 'jobs', 'create']:
    s['create_attempts'] = s.get('create_attempts', 0) + 1
    if s['create_attempts'] <= s.get('propagation_failures', 0):
        error = 'Service account ' + s['account']['email'] + ' does not exist.'
    elif s.get('create_error'):
        error = s['create_error']
elif a[:3] == ['scheduler', 'jobs', 'pause']:
    error = s.get('pause_error')
elif a[:3] == ['scheduler', 'jobs', 'describe']:
    result = s.get('scheduler_state', 'PAUSED')
elif a[:3] != ['run', 'jobs', 'add-iam-policy-binding']:
    error = 'Unexpected command: ' + repr(a)
path.write_text(json.dumps(s))
if error:
    print(error, file=sys.stderr)
    sys.exit(1)
print(result if isinstance(result, str) else json.dumps(result))
'''


def owned_job():
    return {
        "name": "projects/flip-auto/locations/us-central1/jobs/flip-auto-shadow",
        "description": MARKER,
        "schedule": "*/30 * * * *",
        "timeZone": "America/Phoenix",
        "state": "ENABLED", "attemptDeadline": "180s",
        "httpTarget": {
            "uri": "https://run.googleapis.com/v2/projects/flip-auto/locations/us-central1/jobs/flip-auto-shadow:run",
            "httpMethod": "POST", "body": "e30=",
            "headers": {"Content-Type": "application/json"},
            "oauthToken": {"serviceAccountEmail": ACCOUNT,
                           "scope": "https://www.googleapis.com/auth/cloud-platform"},
        },
    }


class SchedulerSetupTests(unittest.TestCase):
    def invoke(self, changes=None, recover=False):
        scenario = {"account": {"email": ACCOUNT, "displayName": "Flip Auto shadow invoker",
                                 "description": MARKER}}
        scenario.update(changes or {})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "scenario.json"
            state.write_text(json.dumps(scenario))
            cli = root / "gcloud"
            cli.write_text(f"#!{sys.executable}\n" + FAKE_GCLOUD)
            cli.chmod(0o755)
            sleep = root / "sleep"
            sleep.write_text("#!/bin/sh\nexit 0\n")
            sleep.chmod(0o755)
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ["PATH"],
                       SCENARIO_PATH=str(state), FLIP_AUTO_GCP_PROJECT_ID="flip-auto",
                       FLIP_AUTO_GCP_REGION="us-central1")
            command = ["bash", str(SCRIPT), "--apply"]
            if recover:
                command.append("--recover-existing-sa")
            result = subprocess.run(command, env=env, capture_output=True, text=True)
            return result, json.loads(state.read_text())

    def assert_no_grant(self, state):
        self.assertFalse(any(call[:3] == ["run", "jobs", "add-iam-policy-binding"]
                             for call in state["calls"]))

    def test_new_account_propagation_retries_then_pauses_before_grant(self):
        result, state = self.invoke({"propagation_failures": 2})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(state["create_attempts"], 3)
        prefixes = [call[:3] for call in state["calls"]]
        self.assertLess(prefixes.index(["scheduler", "jobs", "pause"]),
                        prefixes.index(["scheduler", "jobs", "describe"]))
        self.assertLess(prefixes.index(["scheduler", "jobs", "describe"]),
                        prefixes.index(["run", "jobs", "add-iam-policy-binding"]))
        self.assertIn("Scheduler is PAUSED", result.stdout)
        self.assertFalse(any("resume" in call for call in state["calls"]))

    def test_existing_account_requires_explicit_recovery(self):
        result, state = self.invoke({"account_exists": True})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(state["calls"]), 1)

    def test_recovery_resumes_partial_expected_scheduler_without_recreation(self):
        result, state = self.invoke({"account_exists": True, "jobs": [owned_job()]}, recover=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("create_attempts", state)
        self.assertFalse(any(call[:3] == ["iam", "service-accounts", "create"]
                             for call in state["calls"]))

    def test_recovery_of_paused_scheduler_does_not_pause_again(self):
        job = owned_job()
        job["state"] = "PAUSED"
        result, state = self.invoke({"jobs": [job], "pause_error": "already paused"}, recover=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(call[:3] == ["scheduler", "jobs", "pause"] for call in state["calls"]))
        self.assertTrue(any(call[:3] == ["scheduler", "jobs", "describe"] for call in state["calls"]))

    def test_recovery_rejects_foreign_account_metadata(self):
        result, state = self.invoke({"account": {"email": ACCOUNT, "displayName": "Other"}}, recover=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("metadata mismatch", result.stderr)
        self.assert_no_grant(state)
        self.assertNotIn("create_attempts", state)

    def test_recovery_rejects_wrong_scheduler_target(self):
        job = owned_job()
        job["httpTarget"]["uri"] = "https://example.com/other-job"
        result, state = self.invoke({"jobs": [job]}, recover=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match", result.stderr)
        self.assert_no_grant(state)
        self.assertFalse(any(call[:3] == ["scheduler", "jobs", "pause"] for call in state["calls"]))

    def test_rejects_existing_direct_or_broad_grants(self):
        for filename, member in (("project_policy", "allAuthenticatedUsers"),
                                 ("project_policy", "serviceAccount:" + ACCOUNT),
                                 ("job_policy", "serviceAccount:" + ACCOUNT)):
            with self.subTest(filename=filename, member=member):
                policy = {"bindings": [{"role": "roles/run.invoker", "members": [member]}]}
                result, state = self.invoke({filename: policy}, recover=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("IAM grant found", result.stderr)
                self.assert_no_grant(state)
                self.assertNotIn("create_attempts", state)

    def test_pause_must_succeed_and_be_confirmed(self):
        for changes in ({"pause_error": "permission denied"}, {"scheduler_state": "ENABLED"}):
            with self.subTest(changes=changes):
                result, state = self.invoke(changes)
                self.assertNotEqual(result.returncode, 0)
                self.assert_no_grant(state)

    def test_propagation_retries_are_bounded(self):
        result, state = self.invoke({"propagation_failures": 100})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state["create_attempts"], 7)
        self.assertIn("--recover-existing-sa", result.stderr)
        self.assert_no_grant(state)

    def test_unrelated_creation_failure_is_not_retried(self):
        result, state = self.invoke({"create_error": "permission denied"})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state["create_attempts"], 1)
        self.assert_no_grant(state)


if __name__ == "__main__":
    unittest.main()
