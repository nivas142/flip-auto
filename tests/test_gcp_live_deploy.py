"""Deployment boundaries, drift refusal and pause-before-invoker safety."""
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("prepare_live", Path(__file__).resolve().parents[1] / "deploy/gcp/prepare-live.py")
live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live)
IMAGE = live.REPOSITORY + "@sha256:" + "a" * 64
URL = "https://callback.example.com"
VERSIONS = {key: str(i + 1) for i, key in enumerate(live.SECRETS)}


def job_fixture(v1=False):
    env = [{"name": key, "value": value} for key, value in live.plain_env(URL).items()]
    for key, secret in live.SECRETS.items():
        ref = {"valueFrom": {"secretKeyRef": {"name": secret, "key": VERSIONS[key]}}} if v1 else {
            "valueSource": {"secretKeyRef": {"secret": secret, "version": VERSIONS[key]}}}
        env.append({"name": key, **ref})
    container = {"image": IMAGE, "command": ["python"], "args": ["gcp_live_runtime.py"], "env": env,
                 "resources": {"limits": {"cpu": "1000m", "memory": "1Gi"}}}
    if v1:
        task = {"containers": [container], "serviceAccountName": live.RUNTIME_ACCOUNT,
                "maxRetries": 0, "timeoutSeconds": "900"}
        return {"metadata": {"name": live.JOB}, "spec": {"template": {"spec": {
            "taskCount": 1, "parallelism": 1, "template": {"spec": task}}}}}
    task = {"containers": [container], "serviceAccount": live.RUNTIME_ACCOUNT, "maxRetries": 0, "timeout": "900s"}
    return {"name": f"projects/{live.PROJECT}/locations/{live.REGION}/jobs/{live.JOB}",
            "template": {"taskCount": 1, "parallelism": 1, "template": task}}


def scheduler_fixture(state="PAUSED"):
    return {"name": f"projects/{live.PROJECT}/locations/{live.REGION}/jobs/{live.JOB}", "state": state,
            "description": live.MARKER, "schedule": live.SCHEDULE, "timeZone": "America/Phoenix",
            "attemptDeadline": "180s", "retryConfig": {}, "httpTarget": {
                "uri": f"https://run.googleapis.com/v2/projects/{live.PROJECT}/locations/{live.REGION}/jobs/{live.JOB}:run",
                "httpMethod": "POST", "headers": {"Content-Type": "application/json"}, "body": "e30=",
                "oauthToken": {"serviceAccountEmail": live.SCHEDULER_ACCOUNT,
                               "scope": "https://www.googleapis.com/auth/cloud-platform"}}}


def prepared():
    member = f"serviceAccount:{live.RUNTIME_ACCOUNT}"
    return {"account": {}, "database": {}, "field": {"indexConfig": {}}, "policy": {"bindings": [
        {"members": [member], "role": "roles/datastore.user", "condition": live.DATABASE_CONDITION}]},
        "secrets": {s: {"bindings": [{"members": [member], "role": "roles/secretmanager.secretAccessor"}]}
                    for s in live.SECRETS.values()}}


class FakeCloud:
    def __init__(self):
        self.calls = []
        self.job = None
        self.scheduler = None
        self.granted = False
        self.disable_version = False
        self.pause_fails = False

    def run(self, *args, **kwargs):
        self.calls.append(args)
        if args[:3] == ("secrets", "versions", "describe"):
            return {"name": f"projects/{live.PROJECT_NUMBER}/secrets/{args[4].split('=',1)[1]}/versions/{args[3]}",
                    "state": "DISABLED" if self.disable_version else "ENABLED"}
        if args[:3] == ("run", "jobs", "describe"):
            return copy.deepcopy(self.job)
        if args[:3] == ("run", "jobs", "list"):
            return [copy.deepcopy(self.job)] if self.job is not None else []
        if args[:3] == ("run", "jobs", "create"):
            self.job = job_fixture()
            return {}
        if args[:3] == ("run", "jobs", "get-iam-policy"):
            return {"bindings": [{"members": [f"serviceAccount:{live.SCHEDULER_ACCOUNT}"], "role": "roles/run.invoker"}]} if self.granted else {}
        if args[:3] == ("run", "jobs", "add-iam-policy-binding"):
            self.granted = True
            return {}
        if args[:3] == ("scheduler", "jobs", "describe"):
            return copy.deepcopy(self.scheduler)
        if args[:3] == ("scheduler", "jobs", "create"):
            self.scheduler = scheduler_fixture("ENABLED")
            return {}
        if args[:3] == ("scheduler", "jobs", "pause"):
            if not self.pause_fails:
                self.scheduler["state"] = "PAUSED"
            return {}
        raise AssertionError(args)


class LiveDeploymentTests(unittest.TestCase):
    def operation(self, cloud):
        operation = live.Preparation(cloud)
        operation.inspect = lambda: prepared()
        operation.account = lambda *a, **k: {}
        operation.project = lambda: {}
        return operation

    def test_v1_and_v2_job_readbacks_accept_exact_settings(self):
        for v1 in (False, True):
            live.verify_job(job_fixture(v1), IMAGE, VERSIONS, URL)

    def test_job_refuses_image_secret_and_sidecar_drift(self):
        for change in ("image", "secret", "container", "network", "env", "retry", "command"):
            with self.subTest(change=change):
                raw = job_fixture()
                task = raw["template"]["template"]
                container = task["containers"][0]
                if change == "image":
                    container["image"] = IMAGE.replace("a" * 64, "b" * 64)
                elif change == "secret":
                    container["env"][-1]["valueSource"]["secretKeyRef"]["version"] = "latest"
                elif change == "container":
                    task["containers"].append(copy.deepcopy(container))
                elif change == "network":
                    task["vpcAccess"] = {"connector": "unexpected"}
                elif change == "env":
                    container["env"].append({"name": "EXTRA", "value": "yes"})
                elif change == "retry":
                    task["maxRetries"] = 1
                else:
                    container["args"] = ["gcp_runtime.py"]
                with self.assertRaises(live.PreparationError):
                    live.verify_job(raw, IMAGE, VERSIONS, URL)

    def test_inputs_require_exact_nine_positive_pins_and_no_callback_token(self):
        live.validate_inputs(IMAGE, VERSIONS, URL)
        for versions in ({}, {**VERSIONS, "EXTRA": "1"}, {**VERSIONS, "EMAIL_USERNAME": "latest"},
                         {**VERSIONS, "EMAIL_USERNAME": 1}, {**VERSIONS, "EMAIL_USERNAME": "0"}):
            with self.assertRaises(live.PreparationError):
                live.validate_inputs(IMAGE, versions, URL)
        for url in (URL + "/callback", URL + "?token=secret", "https://user:pass@example.com", "http://example.com", URL + ":99999"):
            with self.assertRaises(live.PreparationError):
                live.validate_inputs(IMAGE, VERSIONS, url)

    def test_read_only_check_has_no_mutations_and_never_reads_payload(self):
        cloud = FakeCloud()
        self.operation(cloud).create_job(IMAGE, VERSIONS, URL)
        self.assertTrue(all(c[2] in ("describe", "get-iam-policy", "list") for c in cloud.calls))
        self.assertFalse(any("access" in c for c in cloud.calls))

    def test_creation_pauses_and_verifies_before_invoker_grant(self):
        cloud = FakeCloud()
        self.operation(cloud).create_job(IMAGE, VERSIONS, URL, apply=True)
        prefixes = [c[:3] for c in cloud.calls]
        pause = prefixes.index(("scheduler", "jobs", "pause"))
        verify = prefixes.index(("scheduler", "jobs", "describe"), pause)
        grant = prefixes.index(("run", "jobs", "add-iam-policy-binding"))
        self.assertLess(pause, verify)
        self.assertLess(verify, grant)
        self.assertEqual(cloud.calls[grant], (
            "run", "jobs", "add-iam-policy-binding", live.JOB, f"--region={live.REGION}",
            f"--member=serviceAccount:{live.SCHEDULER_ACCOUNT}", "--role=roles/run.invoker",
        ))
        self.assertFalse(any("execute" in c or "resume" in c or "access" in c for c in cloud.calls))
        create = next(c for c in cloud.calls if c[:3] == ("run", "jobs", "create"))
        self.assertIn("--max-retries=0", create)
        self.assertIn("--args=gcp_live_runtime.py", create)
        self.assertNotIn("--execute-now", create)

    def test_exact_completed_deployment_can_be_rerun_without_writes(self):
        cloud = FakeCloud()
        cloud.job, cloud.scheduler, cloud.granted = job_fixture(), scheduler_fixture(), True
        self.operation(cloud).create_job(IMAGE, VERSIONS, URL, apply=True)
        self.assertTrue(all(c[2] in ("describe", "get-iam-policy", "list") for c in cloud.calls))

    def test_failed_pause_never_grants_invocation(self):
        cloud = FakeCloud()
        cloud.pause_fails = True
        with self.assertRaises(live.PreparationError):
            self.operation(cloud).create_job(IMAGE, VERSIONS, URL, apply=True)
        self.assertFalse(cloud.granted)

    def test_existing_active_granted_scheduler_is_refused_without_changes(self):
        cloud = FakeCloud()
        cloud.job, cloud.scheduler, cloud.granted = job_fixture(), scheduler_fixture("ENABLED"), True
        with self.assertRaises(live.PreparationError):
            self.operation(cloud).create_job(IMAGE, VERSIONS, URL, apply=True)
        self.assertTrue(all(c[2] in ("describe", "get-iam-policy", "list") for c in cloud.calls))

    def test_disabled_version_stops_before_job_creation(self):
        cloud = FakeCloud()
        cloud.disable_version = True
        with self.assertRaises(live.PreparationError):
            self.operation(cloud).create_job(IMAGE, VERSIONS, URL, apply=True)
        self.assertIsNone(cloud.job)

    def test_permission_denial_is_not_absence(self):
        with patch.object(live.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "PERMISSION_DENIED: no access")):
            with self.assertRaises(live.PreparationError):
                live.Gcloud().run("secrets", "describe", "test", missing_ok=True)

    def test_runtime_grant_is_database_scoped_and_incomplete_prepare_refused(self):
        state = prepared()
        self.operation(FakeCloud()).require_prepared(state)
        state["policy"]["bindings"][0].pop("condition")
        with self.assertRaises(live.PreparationError):
            self.operation(FakeCloud()).require_prepared(state)

    def test_prepare_preflights_everything_before_any_mutation(self):
        cloud = FakeCloud()
        operation = live.Preparation(cloud)
        def drift():
            raise live.PreparationError("Existing database differs")
        operation.inspect = drift
        with self.assertRaises(live.PreparationError):
            operation.prepare(apply=True)
        self.assertEqual(cloud.calls, [])

    def test_prepare_creates_only_new_containers_and_no_workloads(self):
        calls = []
        class RecordCloud:
            def run(self, *args, **kwargs):
                calls.append(args)
                return {}
        operation = live.Preparation(RecordCloud())
        before = {"account": None, "database": None, "field": None, "policy": {},
                  "secrets": {secret: (None if secret in live.NEW_SECRETS else {}) for secret in live.SECRETS.values()}}
        states = iter((before, prepared()))
        operation.inspect = lambda: next(states)
        operation.account = lambda *args, **kwargs: {}
        operation.prepare(apply=True)
        created = [c[2] for c in calls if c[:2] == ("secrets", "create")]
        self.assertEqual(set(created), set(live.NEW_SECRETS))
        self.assertFalse(any(c[0] in ("run", "scheduler") or "versions" in c for c in calls))
        grants = [c for c in calls if c[:2] == ("projects", "add-iam-policy-binding")]
        self.assertEqual(len(grants), 1)
        self.assertIn("--role=roles/datastore.user", grants[0])
        self.assertTrue(any("databases/flip-auto-live" in arg for arg in grants[0]))

    def test_repeating_prepared_phase_makes_no_writes(self):
        cloud = FakeCloud()
        self.operation(cloud).prepare(apply=True)
        self.assertEqual(cloud.calls, [])

    def test_broad_invoker_and_scheduler_extra_permissions_are_refused(self):
        for binding in ({"members": ["allUsers"], "role": "roles/run.invoker"},
                        {"members": [f"serviceAccount:{live.SCHEDULER_ACCOUNT}"], "role": "roles/run.admin"}):
            with self.assertRaises(live.PreparationError):
                live.Preparation.verify_invoker_policy({"bindings": [binding]})


if __name__ == "__main__":
    unittest.main()
