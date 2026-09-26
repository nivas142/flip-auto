from __future__ import annotations

import copy
from contextlib import redirect_stderr
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import subprocess

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("update_shadow_parser", ROOT / "deploy/gcp/update-shadow-parser.py")
rollout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rollout)
NEW_IMAGE = rollout.REPOSITORY + "@sha256:" + "a" * 64


def job(image=rollout.BASE_IMAGE):
    return {
        "metadata": {"name": rollout.JOB},
        "spec": {"template": {"spec": {
            "taskCount": 1, "parallelism": 1,
            "template": {"spec": {
                "serviceAccountName": rollout.SERVICE_ACCOUNT, "maxRetries": 0,
                "timeoutSeconds": "900",
                "containers": [{"image": image, "env": [
                    {"name": "FLIP_AUTO_EXECUTION_MODE", "value": "shadow"},
                    {"name": "EMAIL_APP_PASSWORD", "valueFrom": {"secretKeyRef": {
                        "name": "flip-auto-email-app-password", "key": "1"}}},
                ], "resources": {"limits": {"cpu": "1", "memory": "1Gi"}}}],
            }},
        }}},
    }


def replay_result():
    return {"baseline_verified": True, "module_profile": "current-parser",
            "result_sha256": rollout.RESULT_SHA, "module_sha256": {"cloud_cma": rollout.PARSER_SHA},
            "side_effects": {"alerts_sent": 0, "cma_requests": 0, "persistent_state_writes": 0}}


class FakeCommands:
    def __init__(self):
        self.calls = []
        self.jobs = [job(), job(), job(NEW_IMAGE)]
        self.result = replay_result()
        self.build_files = None
        self.build_dockerfile = None

    def gcloud(self, *args):
        self.calls.append(("gcloud", *args))
        if args[:2] == ("projects", "describe"):
            return {"projectId": rollout.PROJECT, "projectNumber": rollout.PROJECT_NUMBER,
                    "lifecycleState": "ACTIVE"}
        if args[:3] == ("run", "jobs", "describe"):
            return copy.deepcopy(self.jobs.pop(0))
        if args[:3] == ("run", "jobs", "update"):
            return {}
        raise AssertionError("Unexpected cloud action")

    def run(self, *args, **kwargs):
        self.calls.append(args)
        if args[:2] == ("docker", "build"):
            context = Path(args[-1])
            self.build_files = sorted(item.name for item in context.iterdir())
            self.build_dockerfile = (context / "Dockerfile").read_text()
        elif args[:2] == ("docker", "run"):
            return "[CMA_REPLAY] " + json.dumps(self.result)
        elif args[:3] == ("docker", "image", "inspect"):
            return json.dumps([NEW_IMAGE])
        return ""


class ParserRolloutTests(unittest.TestCase):
    def test_baked_parser_is_replayed_before_only_shadow_image_changes(self):
        commands = FakeCommands()
        self.assertEqual(rollout.apply(ROOT, commands, io.StringIO()), NEW_IMAGE)
        self.assertEqual(commands.build_files, ["Dockerfile", "cloud_cma.py"])
        self.assertEqual(commands.build_dockerfile,
                         f"FROM {rollout.BASE_IMAGE}\nCOPY --chown=10001:10001 cloud_cma.py /app/cloud_cma.py\n")
        self.assertIn("--platform=linux/amd64", next(call for call in commands.calls if call[:2] == ("docker", "build")))
        run = next(call for call in commands.calls if call[:2] == ("docker", "run"))
        self.assertEqual(run.count("--mount"), 1)
        self.assertTrue(run[run.index("--mount") + 1].endswith("dst=/app/gcp_cma_replay.py,readonly"))
        self.assertNotIn("--env", run)
        updates = [call for call in commands.calls if call[:4] == ("gcloud", "run", "jobs", "update")]
        self.assertEqual(updates, [("gcloud", "run", "jobs", "update", rollout.JOB,
                                   "--region=us-central1", f"--image={NEW_IMAGE}")])
        self.assertFalse(any("execute" in call or "scheduler" in call for call in commands.calls))
        self.assertLess(commands.calls.index(run), next(i for i, call in enumerate(commands.calls)
                                                       if call[:2] == ("docker", "push")))

    def test_failed_baseline_prevents_push_and_promotion(self):
        commands = FakeCommands()
        commands.result["baseline_verified"] = False
        with self.assertRaisesRegex(rollout.RolloutError, "baseline"):
            rollout.apply(ROOT, commands, io.StringIO())
        self.assertFalse(any("push" in call or "update" in call for call in commands.calls))

    def test_drift_in_secret_reference_prevents_promotion(self):
        commands = FakeCommands()
        task = commands.jobs[1]["spec"]["template"]["spec"]["template"]["spec"]
        task["containers"][0]["env"][1]["valueFrom"]["secretKeyRef"]["key"] = "2"
        with self.assertRaisesRegex(rollout.RolloutError, "configuration changed"):
            rollout.apply(ROOT, commands, io.StringIO())
        self.assertFalse(any("update" in call for call in commands.calls))

    def test_existing_new_image_stops_before_build(self):
        commands = FakeCommands()
        commands.jobs[0] = job(NEW_IMAGE)
        with self.assertRaisesRegex(rollout.RolloutError, "no overwrite"):
            rollout.apply(ROOT, commands, io.StringIO())
        self.assertFalse(any(call[0] == "docker" for call in commands.calls))

    def test_post_update_drift_is_not_reported_as_success(self):
        commands = FakeCommands()
        commands.jobs[2]["spec"]["template"]["spec"]["template"]["spec"]["timeoutSeconds"] = "1200"
        output = io.StringIO()
        with self.assertRaisesRegex(rollout.RolloutError, "readback did not verify"):
            rollout.apply(ROOT, commands, output)
        self.assertNotIn("Verified shadow image:", output.getvalue())

    def test_non_shadow_or_entrypoint_override_rejected(self):
        for change in ("mode", "command"):
            with self.subTest(change=change):
                resource = job()
                container = resource["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]
                if change == "mode":
                    container["env"][0]["value"] = "production"
                else:
                    container["command"] = ["python", "monitor.py"]
                with self.assertRaises(rollout.RolloutError):
                    rollout.job_configuration(resource, rollout.BASE_IMAGE)

    def test_v2_shape_checks_equivalent_shadow_constraints(self):
        resource = job()
        template = copy.deepcopy(resource["spec"]["template"]["spec"])
        template["template"] = template["template"]["spec"]
        task = template["template"]
        task["serviceAccount"] = task.pop("serviceAccountName")
        result = rollout.job_configuration({"name": f"projects/flip-auto/locations/us-central1/jobs/{rollout.JOB}",
                                            "template": template}, rollout.BASE_IMAGE)
        self.assertEqual(result["task"]["serviceAccount"], rollout.SERVICE_ACCOUNT)

    def test_changed_reviewed_source_fails_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            (root / "cloud_cma.py").write_text("unreviewed parser")
            (root / "scripts/gcp_cma_replay.py").write_text("unreviewed replay")
            with self.assertRaisesRegex(rollout.RolloutError, "missing or changed"):
                rollout.reviewed_files(root)

    def test_nonzero_replay_surfaces_bounded_reviewed_diagnostics_only(self):
        result = subprocess.CompletedProcess([], 1,
            stdout='unrelated confidential output\n[CMA_REPLAY_DIAGNOSTIC] {"pypdf_version":"6.19.0"}\n',
            stderr='unrelated Docker error\n[CMA_REPLAY_ERROR] Replay results differ from the reviewed baseline\n')
        output = io.StringIO()
        with patch.object(rollout.subprocess, "run", return_value=result), redirect_stderr(output):
            with self.assertRaisesRegex(rollout.RolloutError, "docker run failed; exit 1"):
                rollout.Commands().run("docker", "run", "reviewed-image")
        self.assertIn("[CMA_REPLAY_DIAGNOSTIC]", output.getvalue())
        self.assertIn("[CMA_REPLAY_ERROR]", output.getvalue())
        self.assertNotIn("unrelated", output.getvalue())


if __name__ == "__main__":
    unittest.main()
