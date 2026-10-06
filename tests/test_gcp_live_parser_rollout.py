from __future__ import annotations

import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "update_live_parser",
    ROOT / "deploy/gcp/update-live-parser.py",
)
rollout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rollout)
NEW_IMAGE = rollout.REPOSITORY + "@sha256:" + "a" * 64


def job(image=rollout.BASE_IMAGE):
    return {
        "metadata": {"name": rollout.JOB},
        "spec": {"template": {"spec": {
            "taskCount": 1,
            "parallelism": 1,
            "template": {"spec": {
                "serviceAccountName": rollout.SERVICE_ACCOUNT,
                "maxRetries": 0,
                "timeoutSeconds": 900,
                "containers": [{
                    "image": image,
                    "command": ["python"],
                    "args": ["gcp_live_runtime.py"],
                    "env": [
                        {"name": "FLIP_AUTO_EXECUTION_MODE", "value": "live"},
                        {"name": "EMAIL_APP_PASSWORD", "valueFrom": {"secretKeyRef": {
                            "name": "flip-auto-email-app-password", "key": "1",
                        }}},
                    ],
                    "resources": {"limits": {"cpu": "1", "memory": "1Gi"}},
                }],
            }},
        }}},
    }


def scheduler(state="PAUSED"):
    return {
        "state": state,
        "httpTarget": {
            "httpMethod": "POST",
            "uri": "https://run.googleapis.com/v2/projects/flip-auto/locations/us-central1/jobs/flip-auto-live:run",
        },
    }


def completed_execution():
    return {"status": {"completionTime": "2026-10-06T08:37:16Z"}}


def recovery_result():
    return {
        "verified": True,
        "parser_version": 4,
        "result_sha256": rollout.RECOVERY_RESULT_SHA,
        "page_count": 86,
        "parsed_closed_comps": 7,
        "eligible_closed_comps": 6,
        "side_effects": {"alerts_sent": 0, "cma_requests": 0, "persistent_state_writes": 0},
    }


class FakeCommands:
    def __init__(self):
        self.calls = []
        self.jobs = [job(), job(), job(NEW_IMAGE)]
        self.schedulers = [scheduler(), scheduler(), scheduler()]
        self.execution_lists = [[completed_execution()], [completed_execution()]]
        self.build_files = None
        self.dockerfile = None

    def gcloud(self, *args):
        self.calls.append(("gcloud", *args))
        if args[:2] == ("projects", "describe"):
            return {"projectId": rollout.PROJECT, "projectNumber": rollout.PROJECT_NUMBER, "lifecycleState": "ACTIVE"}
        if args[:3] == ("run", "jobs", "describe"):
            return copy.deepcopy(self.jobs.pop(0))
        if args[:3] == ("scheduler", "jobs", "describe"):
            return copy.deepcopy(self.schedulers.pop(0))
        if args[:4] == ("run", "jobs", "executions", "list"):
            return copy.deepcopy(self.execution_lists.pop(0))
        if args[:3] == ("run", "jobs", "update"):
            return {}
        raise AssertionError(args)

    def run(self, *args, **_kwargs):
        self.calls.append(args)
        if args[:2] == ("docker", "build"):
            context = Path(args[-1])
            self.build_files = sorted(
                str(path.relative_to(context)) for path in context.rglob("*") if path.is_file()
            )
            self.dockerfile = (context / "Dockerfile").read_text(encoding="utf-8")
        elif args[:2] == ("docker", "run"):
            return "[CMA_RECOVERY] " + json.dumps(recovery_result())
        elif args[:3] == ("docker", "image", "inspect"):
            return json.dumps([NEW_IMAGE])
        return ""


class LiveParserRolloutTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.recovery = Path(self.directory.name) / "recovery.txt"
        self.recovery.write_bytes(b"fixture")

    def apply(self, commands):
        original_verify = rollout.verify_recovery_text
        rollout.verify_recovery_text = lambda _path: None
        self.addCleanup(setattr, rollout, "verify_recovery_text", original_verify)
        return rollout.apply(self.recovery, root=ROOT, commands=commands, output=io.StringIO())

    def test_verified_image_only_update_keeps_scheduler_paused(self):
        commands = FakeCommands()
        self.assertEqual(self.apply(commands), NEW_IMAGE)
        self.assertEqual(
            commands.build_files,
            ["Dockerfile", "cloud_cma.py", "gcp_live_runtime.py", "monitor.py", "scripts/gcp_cma_recovery_verify.py"],
        )
        run = next(call for call in commands.calls if call[:2] == ("docker", "run"))
        self.assertIn("--network=none", run)
        self.assertNotIn("--env", run)
        updates = [call for call in commands.calls if call[:4] == ("gcloud", "run", "jobs", "update")]
        self.assertEqual(updates, [("gcloud", "run", "jobs", "update", rollout.JOB,
                                    "--region=us-central1", f"--image={NEW_IMAGE}")])
        self.assertFalse(any("execute" in call or "resume" in call for call in commands.calls))

    def test_active_execution_stops_before_build(self):
        commands = FakeCommands()
        commands.execution_lists[0] = [{"status": {"startTime": "2026-10-06T09:00:00Z"}}]
        with self.assertRaisesRegex(rollout.RolloutError, "still be active"):
            self.apply(commands)
        self.assertFalse(any(call[:2] == ("docker", "build") for call in commands.calls))

    def test_enabled_scheduler_stops_before_build(self):
        commands = FakeCommands()
        commands.schedulers[0]["state"] = "ENABLED"
        with self.assertRaisesRegex(rollout.RolloutError, "PAUSED"):
            self.apply(commands)
        self.assertFalse(any(call[:2] == ("docker", "build") for call in commands.calls))

    def test_post_update_entrypoint_drift_is_not_success(self):
        commands = FakeCommands()
        commands.jobs[2]["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["args"] = ["monitor.py"]
        with self.assertRaises(rollout.RolloutError):
            self.apply(commands)

    def test_recovery_output_must_match_all_counts_and_side_effects(self):
        result = recovery_result()
        result["eligible_closed_comps"] = 5
        with self.assertRaisesRegex(rollout.RolloutError, "baseline"):
            rollout.verify_recovery_output("[CMA_RECOVERY] " + json.dumps(result))


if __name__ == "__main__":
    unittest.main()
