"""Immutable execution evidence must match every reviewed deployment setting."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("live_execution_cutover", ROOT / "deploy/gcp/live-cutover.py")
cutover = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cutover)
spec = importlib.util.spec_from_file_location("live_execution_prepare", ROOT / "deploy/gcp/prepare-live.py")
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)
IMAGE = prepare.REPOSITORY + "@sha256:" + "a" * 64
URL = "https://callback.example.com"
VERSIONS = {key: str(index + 1) for index, key in enumerate(prepare.SECRETS)}


def execution_fixture(v1):
    env = [{"name": key, "value": value} for key, value in prepare.plain_env(URL).items()]
    for name, secret in prepare.SECRETS.items():
        reference = {"valueFrom": {"secretKeyRef": {"name": secret, "key": VERSIONS[name]}}} if v1 else {
            "valueSource": {"secretKeyRef": {"secret": secret, "version": VERSIONS[name]}}}
        env.append({"name": name, **reference})
    container = {"image": IMAGE, "command": ["python"], "args": ["gcp_live_runtime.py"], "env": env,
                 "resources": {"limits": {"cpu": "1000m", "memory": "1Gi"}}}
    if v1:
        task = {"containers": [container], "serviceAccountName": prepare.RUNTIME_ACCOUNT,
                "maxRetries": 0, "timeoutSeconds": 900}
        return {"metadata": {"name": "flip-auto-live-ab123"}, "spec": {
            "taskCount": 1, "parallelism": 1, "template": {"spec": task}}}
    return {"name": "projects/flip-auto/locations/us-central1/jobs/flip-auto-live/executions/flip-auto-live-ab123",
            "taskCount": 1, "parallelism": 1, "template": {"containers": [container],
            "serviceAccount": prepare.RUNTIME_ACCOUNT, "maxRetries": 0, "timeout": "900s"}}


class LiveExecutionConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.versions_path = Path(self.directory.name) / "versions.json"
        self.versions_path.write_text(json.dumps(VERSIONS))

    def verify(self, execution):
        cutover.verify_execution_configuration(execution, IMAGE, self.versions_path, URL)

    def test_matching_v1_and_v2_executions_are_accepted(self):
        for v1 in (True, False):
            with self.subTest(v1=v1):
                self.verify(execution_fixture(v1))

    def test_same_image_with_changed_secret_pin_is_rejected(self):
        for v1 in (True, False):
            with self.subTest(v1=v1):
                execution = execution_fixture(v1)
                task = execution["spec"]["template"]["spec"] if v1 else execution["template"]
                env = task["containers"][0]["env"][-1]
                reference = env["valueFrom" if v1 else "valueSource"]["secretKeyRef"]
                reference["key" if v1 else "version"] = "999"
                with self.assertRaisesRegex(cutover.CutoverError, "configuration differs"):
                    self.verify(execution)

    def test_same_image_with_changed_runtime_command_is_rejected(self):
        for v1 in (True, False):
            with self.subTest(v1=v1):
                execution = execution_fixture(v1)
                task = execution["spec"]["template"]["spec"] if v1 else execution["template"]
                task["containers"][0]["args"] = ["gcp_runtime.py"]
                with self.assertRaisesRegex(cutover.CutoverError, "configuration differs"):
                    self.verify(execution)

    def test_original_task_count_parallelism_and_timeout_are_preserved(self):
        for v1 in (True, False):
            for field in ("taskCount", "parallelism", "timeout"):
                with self.subTest(v1=v1, field=field):
                    execution = execution_fixture(v1)
                    specification = execution["spec"] if v1 else execution
                    if field == "timeout":
                        task = specification["template"]["spec"] if v1 else specification["template"]
                        task["timeoutSeconds" if v1 else "timeout"] = 1800 if v1 else "1800s"
                    else:
                        specification[field] = 2
                    with self.assertRaisesRegex(cutover.CutoverError, "configuration differs"):
                        self.verify(execution)

    def test_execution_network_annotation_is_not_discarded(self):
        execution = execution_fixture(True)
        execution["metadata"]["annotations"] = {"run.googleapis.com/vpc-access-connector": "unexpected"}
        with self.assertRaisesRegex(cutover.CutoverError, "configuration differs"):
            self.verify(execution)


if __name__ == "__main__":
    unittest.main()
