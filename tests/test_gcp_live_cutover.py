import base64
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("live_cutover", ROOT / "deploy/gcp/live-cutover.py")
cutover = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cutover)


def live_state():
    return {"seen": ["prior-alert"], "cma_requests": {}, "last_run": {
        "mode": "live", "errors": 0, "started_at": "2026-09-29T01:01:00Z",
        "finished_at": "2026-09-29T01:02:00Z"}}


def github_fixture(path):
    if path == "/actions/workflows/monitor.yml":
        return {"path": ".github/workflows/monitor.yml", "state": "disabled_manually"}
    if "status=" in path:
        return {"total_count": 0, "workflow_runs": []}
    if path.endswith("runs?per_page=1"):
        return {"workflow_runs": [{"status": "completed", "conclusion": "success", "head_branch": "main",
                                   "created_at": "2026-09-29T01:00:00Z", "updated_at": "2026-09-29T01:03:00Z"}]}
    if path == "/branches/main":
        return {"commit": {"sha": "a" * 40}}
    if path.startswith("/contents/"):
        return {"encoding": "base64", "content": base64.b64encode(json.dumps(live_state()).encode()).decode()}
    raise AssertionError(path)


class CutoverTests(unittest.TestCase):
    def test_github_gate_requires_disabled_workflow(self):
        def get(path):
            response = github_fixture(path)
            if path == "/actions/workflows/monitor.yml":
                response["state"] = "active"
            return response
        with self.assertRaisesRegex(cutover.CutoverError, "Disable"):
            cutover.verify_github_stopped(get)

    def test_waiting_and_pending_runs_block_handoff(self):
        for status in cutover.NONTERMINAL:
            with self.subTest(status=status):
                def get(path):
                    if f"status={status}&" in path:
                        return {"total_count": 1}
                    return github_fixture(path)
                with self.assertRaisesRegex(cutover.CutoverError, "nonterminal"):
                    cutover.verify_github_stopped(get)

    def test_failed_latest_writer_is_not_treated_as_clean(self):
        def get(path):
            response = github_fixture(path)
            if path.endswith("runs?per_page=1"):
                response["workflow_runs"][0]["conclusion"] = "failure"
            return response
        with self.assertRaisesRegex(cutover.CutoverError, "successfully"):
            cutover.verify_github_stopped(get)

    def test_verified_state_is_canonical_with_provenance(self):
        commit, encoded, digest = cutover.github_state(github_fixture)
        self.assertEqual(commit, "a" * 40)
        self.assertEqual(json.loads(encoded), live_state())
        self.assertEqual(digest, hashlib.sha256(encoded.encode()).hexdigest())

    def test_stale_state_and_shadow_state_rejected(self):
        for modification in ({"finished_at": "2026-09-29T00:02:00Z"}, {"mode": "shadow"}, {"errors": 1}):
            def get(path):
                if path.startswith("/contents/"):
                    state = live_state()
                    state["last_run"].update(modification)
                    return {"encoding": "base64", "content": base64.b64encode(json.dumps(state).encode()).decode()}
                return github_fixture(path)
            with self.subTest(modification=modification), self.assertRaises(cutover.CutoverError):
                cutover.github_state(get)

    def test_main_changing_during_snapshot_rejected(self):
        calls = 0
        def get(path):
            nonlocal calls
            if path == "/branches/main":
                calls += 1
                return {"commit": {"sha": ("a" if calls == 1 else "b") * 40}}
            return github_fixture(path)
        with self.assertRaisesRegex(cutover.CutoverError, "changed"):
            cutover.github_state(get)

    def test_document_create_cannot_overwrite(self):
        with patch.object(cutover, "http") as request:
            cutover.patch_document("token", {"enabled": False}, create=True)
        self.assertIn("currentDocument.exists=false", request.call_args.args[0])
        self.assertEqual(request.call_args.kwargs["body"]["fields"]["enabled"], {"booleanValue": False})

    def test_document_update_requires_compare_and_swap(self):
        with self.assertRaises(cutover.CutoverError), patch.object(cutover, "http") as request:
            cutover.patch_document("token", {"enabled": True})
        request.assert_not_called()
        with patch.object(cutover, "http") as request:
            cutover.patch_document("token", {"enabled": True}, update_time="2026-09-29T01:02:00Z")
        self.assertIn("currentDocument.updateTime=", request.call_args.args[0])
        self.assertIn("updateMask.fieldPaths=enabled", request.call_args.args[0])

    def test_unknown_effect_or_existing_lease_blocks_handoff(self):
        for data in ({"lease_owner": "other"}, {"inflight_effect": {"kind": "telegram_alert"}}):
            with self.assertRaises(cutover.CutoverError):
                cutover.no_running_lease(data)

    def test_authenticated_redirect_is_blocked(self):
        with self.assertRaisesRegex(cutover.CutoverError, "redirects"):
            cutover.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://attacker.example")

    def test_callback_destination_mismatch_stops_before_reading_secret(self):
        args = SimpleNamespace(callback_base_url="https://wrong.example", webhook_secret_version="1")
        raw = {"template": {"template": {"containers": [{"env": [
            {"name": "CLOUD_CMA_CALLBACK_BASE_URL", "value": "https://reviewed.example"}]}]}}}
        with patch.object(cutover, "gcloud", return_value=raw), patch.object(cutover, "call") as secret:
            with self.assertRaisesRegex(cutover.CutoverError, "match"):
                cutover.callback_control(args)
        secret.assert_not_called()

    def test_handoff_rejects_different_live_and_callback_webhook_pins_before_cloud_access(self):
        with tempfile.TemporaryDirectory() as directory:
            versions = Path(directory) / "versions.json"
            versions.write_text(json.dumps({"CLOUD_CMA_WEBHOOK_SECRET": "2"}))
            args = SimpleNamespace(versions=str(versions), webhook_secret_version="1",
                                   image="reviewed-image", callback_base_url="https://reviewed.example")
            for action in (cutover.import_state, cutover.activate, cutover.resume):
                with self.subTest(action=action.__name__), \
                        patch.object(cutover, "verify_job") as job, \
                        patch.object(cutover, "gcloud") as cloud, \
                        patch.object(cutover, "call") as secret, \
                        patch.object(cutover, "github") as github, \
                        patch.object(cutover, "http") as request:
                    with self.assertRaisesRegex(cutover.CutoverError, "Live webhook pin"):
                        action("token", args)
                    job.assert_not_called()
                    cloud.assert_not_called()
                    secret.assert_not_called()
                    github.assert_not_called()
                    request.assert_not_called()

    def test_runtime_pins_require_matching_numeric_strings(self):
        with tempfile.TemporaryDirectory() as directory:
            versions = Path(directory) / "versions.json"
            for stored, supplied in (("1", "latest"), (1, "1"), (None, "1"), ("01", "01")):
                versions.write_text(json.dumps({"CLOUD_CMA_WEBHOOK_SECRET": stored}))
                args = SimpleNamespace(versions=str(versions), webhook_secret_version=supplied,
                                       image="reviewed-image", callback_base_url="https://reviewed.example")
                with self.subTest(stored=stored, supplied=supplied), \
                        patch.object(cutover, "verify_job") as job:
                    with self.assertRaises(cutover.CutoverError):
                        cutover.verify_runtime_pins(args)
                    job.assert_not_called()

    def test_matching_runtime_pin_still_requires_full_live_job_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            versions = Path(directory) / "versions.json"
            versions.write_text(json.dumps({"CLOUD_CMA_WEBHOOK_SECRET": "1"}))
            args = SimpleNamespace(versions=str(versions), webhook_secret_version="1",
                                   image="reviewed-image", callback_base_url="https://reviewed.example")
            with patch.object(cutover, "verify_job") as job:
                cutover.verify_runtime_pins(args)
            job.assert_called_once_with(args.image, args.versions, args.callback_base_url)

    def test_firestore_field_roundtrip_retains_types(self):
        values = {"enabled": False, "schema_version": 1, "lease_owner": None,
                  "imported_at": datetime(2026, 9, 29, tzinfo=timezone.utc),
                  "inflight_effect": {"kind": "cma_request", "key": "a" * 64}}
        self.assertEqual(cutover.decode(cutover.encode(values)), values)


if __name__ == "__main__":
    unittest.main()
