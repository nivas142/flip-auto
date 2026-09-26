from __future__ import annotations

from contextlib import ExitStack
import base64
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


replay = load("gcp_cma_replay", "scripts/gcp_cma_replay.py")
setup = load("run_cma_replay", "deploy/gcp/run-cma-replay.py")
PDF = b"%PDF-unit-fixture"


def payload():
    return {
        "subjectProperty": {"reportAddress": "2010 E Arabian Dr", **replay.EXPECTED_REPORT_SUBJECT},
        "comparables": [
            {
                "status": "closed", "soldPrice": price, "squareFootage": 1625,
                "yearBuilt": 1997, "beds": 4, "baths": 3, "daysOld": 40,
                "mlsNumber": str(index), "formattedAddress": f"{index} Fixture Dr",
            }
            for index, price in enumerate((425_000, 475_000, 495_000, 505_000), 1)
        ],
        "parseDiagnostics": {"pageCount": 5, "closedPageCount": 4, "parsedClosedComparables": 4},
    }


class ReplayTests(unittest.TestCase):
    def setUp(self):
        # Unit fixtures exercise today's imported modules. The real Cloud Run
        # replay independently enforces the pinned deployed-image module hashes.
        current = {
            module.__name__: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
            for module in (replay.cloud_cma, replay.deal_screening, replay.monitor, replay.valuation)
        }
        patcher = patch.object(replay, "EXPECTED_MODULE_SHA256", current)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fixture(self, parsed=None):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(replay, "EXPECTED_REPORT_SHA256", hashlib.sha256(PDF).hexdigest()))
        stack.enter_context(patch.object(replay, "EXPECTED_RESULT_SHA256", ""))
        parser = stack.enter_context(patch.object(
            replay.cloud_cma, "parse_cloud_cma_pdf",
            side_effect=lambda *_args, **_kwargs: copy.deepcopy(parsed or payload()),
        ))
        return stack, parser

    def test_both_notification_decisions_without_external_side_effects(self):
        stack, parser = self.fixture()
        forbidden = []
        for name in (
            "send_alert", "send_sms_alert", "send_telegram_alert", "request_quick_cma",
            "fetch_result", "delete_result", "run_monitor", "scan_email_account",
        ):
            forbidden.append(stack.enter_context(patch.object(
                replay.monitor, name, side_effect=AssertionError("external side effect"),
            )))
        result = replay.run_replay(PDF)
        parser.assert_called_once_with(PDF, requested_address=replay.ADDRESS, as_of=replay.AS_OF)
        self.assertFalse(result["results"]["actual_fixture"]["would_notify"])
        self.assertTrue(result["results"]["synthetic_candidate_fixture"]["would_notify"])
        self.assertTrue(result["results"]["synthetic_candidate_fixture"]["synthetic"])
        self.assertEqual(result["report_subject"]["beds"], 3)
        self.assertEqual(result["approved_subject_override"]["beds"], 4)
        self.assertEqual(result["results"]["eligible_closed_comps"], 4)
        self.assertFalse(result["baseline_verified"])
        for operation in forbidden:
            operation.assert_not_called()

    def test_module_mismatch_stops_before_report_access(self):
        with patch.object(replay, "EXPECTED_MODULE_SHA256", {}), patch.object(replay.cloud_cma, "download_cloud_cma_pdf") as download:
            with self.assertRaisesRegex(replay.ReplayError, "modules differ"):
                replay.run_replay()
            download.assert_not_called()

    def test_pdf_hash_mismatch_stops_before_parser(self):
        with patch.object(replay.cloud_cma, "parse_cloud_cma_pdf") as parser:
            with self.assertRaisesRegex(replay.ReplayError, "baseline hash"):
                replay.run_replay(PDF)
            parser.assert_not_called()

    def test_missing_or_mismatched_subject_fails_before_valuation(self):
        for update in ({"reportAddress": ""}, {"reportAddress": "999 Wrong Dr"}, {"squareFootage": 1800}, {"beds": 4}):
            with self.subTest(update=update):
                parsed = payload()
                parsed["subjectProperty"].update(update)
                stack, _ = self.fixture(parsed)
                calculate = stack.enter_context(patch.object(replay.valuation, "calculate_comp_valuation"))
                with self.assertRaises(replay.ReplayError):
                    replay.run_replay(PDF)
                calculate.assert_not_called()
                stack.close()

    def test_insufficient_comps_and_changed_results_are_failures(self):
        parsed = payload()
        parsed["comparables"] = parsed["comparables"][:2]
        stack, _ = self.fixture(parsed)
        with self.assertRaisesRegex(replay.ReplayError, "three eligible"):
            replay.run_replay(PDF)
        stack.close()
        self.fixture()
        with patch.object(replay, "EXPECTED_RESULT_SHA256", "0" * 64):
            with self.assertRaisesRegex(replay.ReplayError, "results differ"):
                replay.run_replay(PDF)

    def test_matched_baseline_enables_parity_marker(self):
        self.fixture()
        result_hash = replay.run_replay(PDF)["result_sha256"]
        with patch.object(replay, "EXPECTED_RESULT_SHA256", result_hash):
            result = replay.run_replay(PDF)
        self.assertTrue(result["baseline_verified"])

    def test_mismatch_logs_evidence_and_still_exits_nonzero(self):
        self.fixture()
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(replay, "EXPECTED_RESULT_SHA256", "0" * 64), \
                patch.object(replay.cloud_cma, "download_cloud_cma_pdf", return_value=PDF), \
                patch("sys.stdout", output), patch("sys.stderr", errors):
            self.assertEqual(replay.main(), 1)
        line = output.getvalue().strip()
        self.assertTrue(line.startswith("[CMA_REPLAY_DIAGNOSTIC] "))
        result = json.loads(line.removeprefix("[CMA_REPLAY_DIAGNOSTIC] "))
        self.assertFalse(result["baseline_verified"])
        self.assertEqual(result["expected_result_sha256"], "0" * 64)
        self.assertNotEqual(result["result_sha256"], result["expected_result_sha256"])
        self.assertEqual(result["result_sha256"], replay.digest(result["results"]))
        self.assertEqual(len(result["selected_comparables"]), 4)
        self.assertEqual(result["selected_comparables"][0]["year_built"], 1997)
        self.assertIn("pypdf_version", result)
        self.assertIn("python_version", result)
        self.assertEqual(result["side_effects"]["alerts_sent"], 0)
        self.assertNotIn(replay.REPORT_URL, line)
        self.assertNotIn("[CMA_REPLAY]", output.getvalue())
        self.assertIn("results differ", errors.getvalue())

    def test_original_deployed_parser_remains_checked_against_same_result(self):
        self.fixture()
        source_hash = replay.EXPECTED_MODULE_SHA256["cloud_cma"]
        expected_hash = replay.run_replay(PDF)["result_sha256"]
        current_profile = {**replay.EXPECTED_MODULE_SHA256, "cloud_cma": "1" * 64}
        with patch.object(replay, "EXPECTED_MODULE_SHA256", current_profile), \
                patch.object(replay, "DEPLOYED_CLOUD_CMA_SHA256", source_hash), \
                patch.object(replay, "EXPECTED_RESULT_SHA256", expected_hash):
            result = replay.run_replay(PDF)
            self.assertEqual(result["module_profile"], "original-deployed-parser")
            self.assertTrue(result["baseline_verified"])
            with patch.object(replay, "EXPECTED_RESULT_SHA256", "0" * 64):
                with self.assertRaisesRegex(replay.ReplayError, "results differ"):
                    replay.run_replay(PDF)

    def test_unexpected_failure_is_nonzero_and_redacted(self):
        output = io.StringIO()
        with patch.object(replay, "run_replay", side_effect=ValueError("private-url-token")), patch("sys.stderr", output):
            self.assertEqual(replay.main(), 1)
        self.assertIn("ValueError", output.getvalue())
        self.assertNotIn("private-url-token", output.getvalue())


class FakeGcloud:
    def __init__(self):
        self.calls = []
        self.project = {"projectId": setup.PROJECT, "projectNumber": setup.PROJECT_NUMBER, "lifecycleState": "ACTIVE"}
        self.jobs = [{"metadata": {"name": "flip-auto-shadow"}}]
        self.image = setup.IMAGE
        self.execution = {
            "metadata": {"name": setup.JOB + "-abcde", "creationTimestamp": "2026-09-26T16:45:00Z",
                         "labels": {"run.googleapis.com/job": setup.JOB}},
            "spec": {"template": {"spec": {"containers": [{"image": setup.IMAGE}]}}},
            "status": {"succeededCount": 1, "conditions": [{"type": "Completed", "status": "True"}],
                       "startTime": "2026-09-26T16:45:10Z", "completionTime": "2026-09-26T16:46:10Z"},
        }
        self.result = {"baseline_verified": True}
        self.logs = [{"textPayload": "[CMA_REPLAY] " + json.dumps(self.result)}]

    def run(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if args[:2] == ("projects", "describe"):
            return copy.deepcopy(self.project)
        if args[:3] == ("run", "jobs", "list"):
            return copy.deepcopy(self.jobs)
        if args[:3] == ("run", "jobs", "describe"):
            return {"spec": {"template": {"spec": {"template": {"spec": {"containers": [{"image": self.image}]}}}}}}
        if args[:3] == ("run", "jobs", "create"):
            return {}
        if args[:3] == ("run", "jobs", "execute"):
            return {}
        if args[:4] == ("run", "jobs", "executions", "list"):
            return [copy.deepcopy(self.execution)]
        if args[:4] == ("run", "jobs", "executions", "describe"):
            return copy.deepcopy(self.execution)
        if args[:2] == ("logging", "read"):
            return copy.deepcopy(self.logs)
        raise AssertionError(args)


class ReplaySetupTests(unittest.TestCase):
    def test_offline_check_has_no_gcloud_calls(self):
        with patch.object(setup.subprocess, "run") as run, patch("sys.stdout", io.StringIO()):
            self.assertEqual(setup.main(["--check"]), 0)
            run.assert_not_called()

    def test_only_fixed_separate_job_created_and_executed_once(self):
        cloud = FakeGcloud()
        output = io.StringIO()
        source = setup.read_source()
        result = setup.apply(source, cloud, output)
        self.assertTrue(result["baseline_verified"])
        create = [args for args, _ in cloud.calls if args[:3] == ("run", "jobs", "create")]
        execute = [args for args, _ in cloud.calls if args[:3] == ("run", "jobs", "execute")]
        self.assertEqual(len(create), 1)
        self.assertEqual(len(execute), 1)
        args = create[0]
        self.assertEqual(args[3], "flip-auto-cma-replay")
        for option in ("--command=python", "--tasks=1", "--parallelism=1", "--max-retries=0", "--task-timeout=900s", "--cpu=1", "--memory=1Gi", "--image=" + setup.IMAGE):
            self.assertIn(option, args)
        self.assertFalse(any("secret" in item or "--wait" == item or "--execute-now" == item for item in args))
        encoded_command = next(value for value in args if value.startswith("--args="))
        encoded = encoded_command.split("base64.b64decode('", 1)[1].split("'", 1)[0]
        self.assertEqual(base64.b64decode(encoded), source)
        self.assertIn("--wait", execute[0])
        log_filter = next(args[2] for args, _ in cloud.calls if args[:2] == ("logging", "read"))
        self.assertIn('execution_name"="flip-auto-cma-replay-abcde"', log_filter)
        self.assertFalse(any("scheduler" in args or "delete" in args or "update" in args for args, _ in cloud.calls))

    def test_existing_job_or_wrong_project_or_wrong_image_prevents_every_write(self):
        for change in ("existing", "project", "image"):
            with self.subTest(change=change):
                cloud = FakeGcloud()
                if change == "existing":
                    cloud.jobs.append({"name": "projects/flip-auto/locations/us-central1/jobs/" + setup.JOB})
                elif change == "project":
                    cloud.project["projectNumber"] = "1234"
                else:
                    cloud.image = "unreviewed-image"
                with self.assertRaises(setup.SetupError):
                    setup.apply(setup.read_source(), cloud, io.StringIO())
                self.assertFalse(any(args[:3] == ("run", "jobs", "create") for args, _ in cloud.calls))

    def test_failed_execution_and_missing_result_logs_do_not_report_success(self):
        cloud = FakeGcloud()
        cloud.execution["status"]["succeededCount"] = 0
        with self.assertRaisesRegex(setup.SetupError, "did not complete"):
            setup.apply(setup.read_source(), cloud, io.StringIO())
        cloud = FakeGcloud()
        cloud.logs = []
        with self.assertRaisesRegex(setup.SetupError, "No single replay result"):
            setup.apply(setup.read_source(), cloud, io.StringIO(), lambda _: None)
        cloud = FakeGcloud()
        cloud.logs = [{"textPayload": '[CMA_REPLAY] {"baseline_verified": false}'}]
        with self.assertRaisesRegex(setup.SetupError, "without confirming"):
            setup.apply(setup.read_source(), cloud, io.StringIO())

    def test_execute_wait_failure_still_reads_only_that_executions_logs(self):
        cloud = FakeGcloud()
        original_run = cloud.run

        def run(*args, **kwargs):
            result = original_run(*args, **kwargs)
            return None if args[:3] == ("run", "jobs", "execute") else result

        cloud.run = run
        output = io.StringIO()
        with self.assertRaisesRegex(setup.SetupError, "Execute wait did not confirm"):
            setup.apply(setup.read_source(), cloud, output)
        self.assertIn("[CMA_REPLAY]", output.getvalue())
        self.assertNotIn("completed. Normal shadow state", output.getvalue())

    def test_execute_wait_timeout_allows_scoped_diagnostics(self):
        with patch.object(setup.subprocess, "run", side_effect=subprocess.TimeoutExpired("gcloud", 1200)):
            self.assertIsNone(setup.Gcloud().run("run", "jobs", "execute", setup.JOB, allow_failure=True))

    def test_gcloud_permission_failure_is_not_absence(self):
        denied = subprocess.CompletedProcess([], 1, "", "PERMISSION_DENIED private-detail")
        with patch.object(setup.subprocess, "run", return_value=denied) as run:
            with self.assertRaisesRegex(setup.SetupError, "exit 1") as caught:
                setup.apply(setup.read_source(), output=io.StringIO())
        self.assertEqual(run.call_count, 1)
        self.assertNotIn("private-detail", str(caught.exception))

    def test_inspect_is_read_only_with_bounded_descending_logs(self):
        cloud = FakeGcloud()
        output = io.StringIO()
        result = setup.inspect_execution(setup.JOB + "-abcde", cloud, output)
        self.assertTrue(result["baseline_verified"])
        self.assertEqual([args[:2] for args, _ in cloud.calls], [
            ("projects", "describe"), ("run", "jobs"), ("logging", "read"),
        ])
        self.assertEqual(cloud.calls[1][0][:4], ("run", "jobs", "executions", "describe"))
        args, options = cloud.calls[-1]
        self.assertIn('--order=desc', args)
        self.assertIn('--limit=10', args)
        self.assertEqual(options["timeout"], 35)
        self.assertIn('timestamp>="2026-09-26T16:43:10Z"', args[2])
        self.assertIn('timestamp<="2026-09-26T16:48:10Z"', args[2])
        self.assertLess(output.getvalue().index("task succeeded"), output.getvalue().index("[CMA_REPLAY]"))

    def test_inspect_cli_does_not_need_an_adjacent_replay_source(self):
        # Importing a directly downloaded /tmp helper must not resolve a repo
        # ancestor or require scripts/gcp_cma_replay.py before parsing --inspect.
        namespace = {"__file__": "/tmp/flip-auto-replay-inspect.py", "__name__": "standalone_replay"}
        source = (ROOT / "deploy/gcp/run-cma-replay.py").read_text()
        exec(compile(source, namespace["__file__"], "exec"), namespace)
        with patch.object(setup, "inspect_execution", return_value={"baseline_verified": True}) as inspect:
            with patch.dict(namespace, {"inspect_execution": inspect}), patch("sys.stdout", io.StringIO()):
                self.assertEqual(namespace["main"](["--inspect", setup.JOB + "-abcde"]), 0)
            inspect.assert_called_once_with(setup.JOB + "-abcde")

    def test_inspect_rejects_wrong_resource_before_log_access(self):
        for change in ("name", "label", "timestamp", "project"):
            with self.subTest(change=change):
                cloud = FakeGcloud()
                if change == "name":
                    cloud.execution["metadata"]["name"] = "flip-auto-shadow-abcde"
                elif change == "label":
                    cloud.execution["metadata"]["labels"]["run.googleapis.com/job"] = "flip-auto-shadow"
                elif change == "timestamp":
                    cloud.execution["status"]["startTime"] = "2026-09-26T16:45:10"
                else:
                    cloud.project["projectNumber"] = "123"
                with self.assertRaisesRegex(setup.SetupError, "--inspect flip-auto-cma-replay-abcde"):
                    setup.inspect_execution(setup.JOB + "-abcde", cloud, io.StringIO())
                self.assertFalse(any(args[:2] == ("logging", "read") for args, _ in cloud.calls))
        cloud = FakeGcloud()
        with self.assertRaises(setup.SetupError):
            setup.inspect_execution("flip-auto-shadow-abcde", cloud, io.StringIO())
        self.assertEqual(cloud.calls, [])

    def test_unverified_image_allows_scoped_diagnostics_but_never_success(self):
        for change, message in (
            ("missing_image", "image is absent"),
            ("missing_spec", "image is absent"),
            ("different_image", "image differs"),
            ("extra_container", "Expected one replay execution container"),
        ):
            with self.subTest(change=change):
                cloud = FakeGcloud()
                output = io.StringIO()
                containers = cloud.execution["spec"]["template"]["spec"]["containers"]
                if change == "missing_image":
                    containers[0].pop("image")
                elif change == "missing_spec":
                    cloud.execution.pop("spec")
                elif change == "different_image":
                    containers[0]["image"] = "different-image"
                else:
                    containers.append({"image": setup.IMAGE})
                with self.assertRaisesRegex(setup.SetupError, message):
                    setup.inspect_execution(setup.JOB + "-abcde", cloud, output)
                logs = [args for args, _ in cloud.calls if args[:2] == ("logging", "read")]
                self.assertEqual(len(logs), 1)
                self.assertIn('execution_name"="flip-auto-cma-replay-abcde"', logs[0][2])
                self.assertIn(message, output.getvalue())
                self.assertIn("[CMA_REPLAY]", output.getvalue())
                self.assertNotIn("completed. Normal shadow state", output.getvalue())
                self.assertFalse(any(args[:3] in {
                    ("run", "jobs", "execute"), ("run", "jobs", "update"),
                    ("run", "jobs", "create"), ("run", "jobs", "delete"),
                } for args, _ in cloud.calls))

    def test_failed_execution_with_omitted_image_exposes_mismatch_diagnostics(self):
        cloud = FakeGcloud()
        output = io.StringIO()
        cloud.execution["spec"]["template"]["spec"]["containers"][0].pop("image")
        cloud.execution["status"].update(
            succeededCount=0, failedCount=1,
            conditions=[{"type": "Completed", "status": "False"}],
        )
        diagnostic = '[CMA_REPLAY_DIAGNOSTIC] {"baseline_verified": false, "result_sha256": "fixture"}'
        error = "[CMA_REPLAY_ERROR] Replay results differ from the reviewed baseline"
        cloud.logs = [{"textPayload": error}, {"textPayload": diagnostic}]
        with self.assertRaisesRegex(setup.SetupError, "did not complete successfully"):
            setup.inspect_execution(setup.JOB + "-abcde", cloud, output)
        text = output.getvalue()
        self.assertIn("failedCount=1", text)
        self.assertIn("image is absent", text)
        self.assertNotIn("image differs", text)
        self.assertIn(diagnostic, text)
        self.assertIn(error, text)
        self.assertLess(text.index("failedCount=1"), text.index(diagnostic))
        query = next(args[2] for args, _ in cloud.calls if args[:2] == ("logging", "read"))
        self.assertIn('textPayload:"[CMA_REPLAY_DIAGNOSTIC]"', query)
        self.assertNotIn("completed. Normal shadow state", text)

    def test_diagnostic_log_cannot_be_hidden_by_a_verified_result(self):
        cloud = FakeGcloud()
        cloud.logs.append({"textPayload": '[CMA_REPLAY_DIAGNOSTIC] {"baseline_verified": false}'})
        with self.assertRaisesRegex(setup.SetupError, "emitted mismatch diagnostics"):
            setup.inspect_execution(setup.JOB + "-abcde", cloud, io.StringIO())

    def test_log_timeout_reports_task_status_but_never_replay_success(self):
        cloud = FakeGcloud()
        original = cloud.run
        output = io.StringIO()

        def timed_out(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[:2] == ("logging", "read"):
                self.assertIn("task succeeded", output.getvalue())
                raise setup.GcloudTimeout("timed out")
            return result

        cloud.run = timed_out
        with self.assertRaisesRegex(setup.SetupError, "baseline remains unconfirmed.*--inspect"):
            setup.inspect_execution(setup.JOB + "-abcde", cloud, output, lambda _: None)
        self.assertEqual(sum(args[:2] == ("logging", "read") for args, _ in cloud.calls), 2)
        self.assertNotIn("completed. Normal shadow state", output.getvalue())

    def test_log_timeout_retries_read_once_and_can_verify_existing_result(self):
        cloud = FakeGcloud()
        original = cloud.run

        def timed_out_once(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[:2] == ("logging", "read") and sum(
                call[:2] == ("logging", "read") for call, _ in cloud.calls
            ) == 1:
                raise setup.GcloudTimeout("timed out")
            return result

        cloud.run = timed_out_once
        self.assertTrue(setup.inspect_execution(
            setup.JOB + "-abcde", cloud, io.StringIO(), lambda _: None,
        )["baseline_verified"])
        logs = [args for args, _ in cloud.calls if args[:2] == ("logging", "read")]
        self.assertEqual(len(logs), 2)
        self.assertEqual(logs[0], logs[1])

    def test_log_permission_denial_is_not_retried_or_reported_as_success(self):
        cloud = FakeGcloud()
        original = cloud.run
        output = io.StringIO()

        def denied(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[:2] == ("logging", "read"):
                raise setup.SetupError("gcloud logging read failed; exit 1")
            return result

        cloud.run = denied
        with self.assertRaisesRegex(setup.SetupError, "failed; exit 1.*--inspect"):
            setup.inspect_execution(setup.JOB + "-abcde", cloud, output, lambda _: None)
        self.assertEqual(sum(args[:2] == ("logging", "read") for args, _ in cloud.calls), 1)
        self.assertNotIn("completed. Normal shadow state", output.getvalue())

    def test_creation_timestamp_fallback_still_bounds_pending_execution_logs(self):
        cloud = FakeGcloud()
        cloud.execution["status"].pop("startTime")
        self.assertEqual(setup.log_time_bounds(cloud.execution), (
            "2026-09-26T16:43:00Z", "2026-09-26T16:48:10Z",
        ))

    def test_error_log_cannot_be_hidden_by_a_baseline_result(self):
        cloud = FakeGcloud()
        cloud.logs.append({"textPayload": "[CMA_REPLAY_ERROR] fixture failure"})
        with self.assertRaisesRegex(setup.SetupError, "emitted an error"):
            setup.inspect_execution(setup.JOB + "-abcde", cloud, io.StringIO())


if __name__ == "__main__":
    unittest.main()
