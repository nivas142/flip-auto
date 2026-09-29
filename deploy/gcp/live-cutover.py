#!/usr/bin/env python3
"""Operator-run production handoff. No action implicitly runs a monitor job.

Uses authenticated gcloud in Cloud Shell. GitHub reads use the public API or an
existing GH_TOKEN/GITHUB_TOKEN environment variable; tokens are never printed.
No remote response bodies or secret payloads are included in diagnostics.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import runpy
import subprocess
import sys
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from gcp_live_runtime import serialize_state, validate_import_state

PROJECT = "flip-auto"
PROJECT_NUMBER = "941818435041"
REGION = "us-central1"
JOB = "flip-auto-live"
REPO = "nivas142/flip-auto"
API = f"https://api.github.com/repos/{REPO}"
DOC = f"projects/{PROJECT}/databases/flip-auto-live/documents/flip_auto_live_state/monitor"
DOC_URL = "https://firestore.googleapis.com/v1/" + DOC
NONTERMINAL = ("queued", "in_progress", "waiting", "requested", "pending")
IMAGE_RE = re.compile(r"us-central1-docker\.pkg\.dev/flip-auto/flip-auto/monitor@sha256:[a-f0-9]{64}")


class CutoverError(RuntimeError):
    """Only fixed safe diagnostics reach the terminal."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CutoverError("Authenticated HTTP redirects are refused")


def require(ok, message):
    if not ok:
        raise CutoverError(message)


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(result.tzinfo is not None, "Timestamp is missing its timezone")
        return result
    except (ValueError, TypeError, AttributeError):
        raise CutoverError("Invalid timestamp in handoff evidence") from None


def call(*args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, check=False, timeout=180)
    except (OSError, subprocess.SubprocessError):
        raise CutoverError("Local command could not complete; no automatic retry") from None
    require(result.returncode == 0, "Local command failed; no automatic retry")
    return result.stdout


def gcloud(*args):
    raw = call("gcloud", *args, f"--project={PROJECT}", "--quiet", "--format=json",
               "--verbosity=error", "--no-log-http")
    try:
        return json.loads(raw) if raw.strip() else {}
    except ValueError:
        raise CutoverError("Unexpected gcloud JSON response") from None


def http(url, *, method="GET", token=None, body=None, absent_ok=False):
    headers = {"User-Agent": "flip-auto-cutover/1.0", "Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, allow_nan=False).encode()
    try:
        with build_opener(NoRedirect()).open(Request(url, data=data, headers=headers, method=method), timeout=45) as response:
            raw = response.read(2 * 1024 * 1024)
        return json.loads(raw) if raw else {}
    except HTTPError as exc:
        if absent_ok and exc.code == 404:
            return None
        raise CutoverError(f"Remote request failed (HTTP {exc.code}); response content withheld") from None
    except (URLError, TimeoutError, ValueError, OSError):
        raise CutoverError("Remote request failed; outcome may be uncertain, inspect before retrying") from None


def github(path):
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    return http(API + path, token=token)


def verify_github_stopped(get=github):
    workflow = get("/actions/workflows/monitor.yml")
    require(workflow.get("path") == ".github/workflows/monitor.yml"
            and workflow.get("state") == "disabled_manually",
            "Disable Flip Auto Monitor in GitHub before transferring production ownership")
    for status in NONTERMINAL:
        runs = get("/actions/workflows/monitor.yml/runs?" + urlencode({"status": status, "per_page": 1}))
        require(runs.get("total_count") == 0, "GitHub still has nonterminal monitor runs; wait for them to finish")
    recent = get("/actions/workflows/monitor.yml/runs?per_page=1").get("workflow_runs", [])
    require(len(recent) == 1 and recent[0].get("status") == "completed"
            and recent[0].get("conclusion") == "success" and recent[0].get("head_branch") == "main",
            "The latest GitHub monitor run must have completed successfully before state handoff")
    return recent[0]


def github_state(get=github):
    run = verify_github_stopped(get)
    commit = get("/branches/main").get("commit", {}).get("sha", "")
    require(bool(re.fullmatch(r"[0-9a-f]{40}", commit)), "Invalid GitHub main commit")
    blob = get(f"/contents/state/monitor_state.json?ref={commit}")
    try:
        require(blob.get("encoding") == "base64", "Unexpected GitHub state encoding")
        state = validate_import_state(json.loads(base64.b64decode(blob["content"], validate=False)))
        last = state["last_run"]
        require(last.get("errors") == 0, "Last GitHub scan has unresolved scan errors")
        require(timestamp(run["created_at"]) <= timestamp(last["started_at"])
                <= timestamp(last["finished_at"]) <= timestamp(run["updated_at"]),
                "GitHub state timestamps do not match the last successful monitor run")
    except (KeyError, ValueError, TypeError):
        raise CutoverError("GitHub production state failed validation") from None
    verify_github_stopped(get)
    require(get("/branches/main").get("commit", {}).get("sha") == commit,
            "GitHub main changed during state verification; retry the read-only check")
    encoded = serialize_state(state)
    return commit, encoded, hashlib.sha256(encoded.encode()).hexdigest()


def encode(value):
    if value is None:
        return {"nullValue": None}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"integerValue": str(value)}
    if isinstance(value, datetime):
        return {"timestampValue": value.isoformat()}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, dict):
        return {"mapValue": {"fields": {key: encode(item) for key, item in value.items()}}}
    raise CutoverError("Unsupported Firestore field type")


def decode(field):
    for kind in ("stringValue", "booleanValue", "integerValue", "timestampValue", "nullValue", "mapValue"):
        if kind in field:
            value = field[kind]
            if kind == "integerValue":
                return int(value)
            if kind == "timestampValue":
                return timestamp(value)
            if kind == "mapValue":
                return {key: decode(item) for key, item in value.get("fields", {}).items()}
            return value
    raise CutoverError("Unexpected Firestore field type")


def read_document(token, *, absent_ok=False):
    raw = http(DOC_URL, token=token, absent_ok=absent_ok)
    if raw is None:
        return None, None
    require(raw.get("name") == DOC, "Unexpected production state document")
    data = {key: decode(value) for key, value in raw.get("fields", {}).items()}
    require(data.get("execution_mode") == "live" and data.get("schema_version") == 1,
            "Unexpected production state schema")
    return data, raw.get("updateTime")


def patch_document(token, fields, *, update_time=None, create=False):
    query = []
    if create:
        query.append(("currentDocument.exists", "false"))
    else:
        require(bool(update_time), "Missing production state concurrency token")
        query.append(("currentDocument.updateTime", update_time))
        query.extend(("updateMask.fieldPaths", key) for key in fields)
    return http(DOC_URL + "?" + urlencode(query), method="PATCH", token=token,
                body={"name": DOC, "fields": {key: encode(value) for key, value in fields.items()}})


def no_running_lease(data):
    require(data.get("lease_owner") is None, "A production lease exists; inspect/drain before changing ownership")
    require(data.get("inflight_effect") is None, "An uncertain side effect requires reconciliation; never automatically clear it")


def verify_job(image, versions_path, callback_url):
    require(bool(IMAGE_RE.fullmatch(image or "")), "Supply the reviewed pinned --image digest")
    raw = gcloud("run", "jobs", "describe", JOB, f"--region={REGION}")
    require(bool(versions_path), "Supply the reviewed --versions JSON file")
    versions = json.loads(Path(versions_path).read_text())
    preparation = runpy.run_path(str(ROOT / "deploy/gcp/prepare-live.py"))
    preparation["validate_inputs"](image, versions, callback_url)
    preparation["verify_job"](raw, image, versions, callback_url)
    try:
        if "spec" in raw:
            execution = raw["spec"]["template"]["spec"]
            task = execution["template"]["spec"]
            account = task["serviceAccountName"]
        else:
            execution = raw["template"]
            task = execution["template"]
            account = task["serviceAccount"]
        name = raw.get("metadata", {}).get("name") or raw.get("name", "")
        require(name.rsplit("/", 1)[-1] == JOB, "Unexpected production job")
        require(len(task["containers"]) == 1, "Expected one production container")
        container = task["containers"][0]
        env = {item["name"]: item.get("value") for item in container.get("env", [])}
        require(container["image"] == image and account == f"{JOB}@{PROJECT}.iam.gserviceaccount.com"
                and container.get("command") == ["python"] and container.get("args") == ["gcp_live_runtime.py"]
                and env.get("FLIP_AUTO_EXECUTION_MODE") == "live"
                and env.get("FIRESTORE_DATABASE_ID") == "flip-auto-live"
                and execution.get("taskCount") == 1 and execution.get("parallelism") == 1
                and task.get("maxRetries") == 0,
                "Production job differs from the reviewed deployment")
    except (KeyError, TypeError, AttributeError):
        raise CutoverError("Unexpected Cloud Run job schema") from None


def verify_runtime_pins(args):
    require(bool(args.versions), "Supply the reviewed --versions JSON file")
    require(bool(re.fullmatch(r"[1-9][0-9]*", args.webhook_secret_version or "")),
            "Supply the existing pinned numeric --webhook-secret-version")
    try:
        versions = json.loads(Path(args.versions).read_text())
    except (OSError, ValueError, TypeError):
        raise CutoverError("Cannot read the reviewed secret versions JSON") from None
    require(isinstance(versions, dict)
            and versions.get("CLOUD_CMA_WEBHOOK_SECRET") == args.webhook_secret_version,
            "Live webhook pin must match the callback-control credential version")
    verify_job(args.image, args.versions, args.callback_base_url)


def scheduler_state():
    raw = gcloud("scheduler", "jobs", "describe", JOB, f"--location={REGION}")
    preparation = runpy.run_path(str(ROOT / "deploy/gcp/prepare-live.py"))
    preparation["verify_scheduler"](raw, paused_only=False)
    return raw.get("state")


def callback_control(args, mode=None):
    parsed = urlsplit(args.callback_base_url or "")
    require(parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password
            and parsed.port in (None, 443) and parsed.path in ("", "/") and not parsed.query and not parsed.fragment,
            "Supply an HTTPS callback base URL without credentials or paths")
    require(bool(re.fullmatch(r"[1-9][0-9]*", args.webhook_secret_version or "")),
            "Supply the existing pinned numeric --webhook-secret-version")
    shadow = gcloud("run", "jobs", "describe", "flip-auto-shadow", f"--region={REGION}")
    task = (shadow["spec"]["template"]["spec"]["template"]["spec"] if "spec" in shadow
            else shadow["template"]["template"])
    containers = task.get("containers", [])
    require(len(containers) == 1, "Unexpected shadow callback configuration")
    settings = {item["name"]: item for item in containers[0].get("env", [])}
    require(settings.get("CLOUD_CMA_CALLBACK_BASE_URL", {}).get("value", "").rstrip("/")
            == args.callback_base_url.rstrip("/"), "Callback URL must match the verified shadow job")
    credential = settings.get("CLOUD_CMA_WEBHOOK_SECRET", {})
    ref = (credential.get("valueFrom", {}) or credential.get("valueSource", {})).get("secretKeyRef", {})
    require((ref.get("name") or ref.get("secret")) == "flip-auto-cma-webhook-secret"
            and (ref.get("key") or ref.get("version")) == args.webhook_secret_version,
            "Webhook version must match the verified shadow job binding")
    secret = call("gcloud", "secrets", "versions", "access", args.webhook_secret_version,
                  "--secret=flip-auto-cma-webhook-secret", f"--project={PROJECT}",
                  "--quiet", "--verbosity=error", "--no-log-http").strip()
    require(len(secret) >= 32, "Webhook credential failed validation")
    endpoint = args.callback_base_url.rstrip("/") + "/control/monitor-dispatch"
    if mode is not None:
        http(endpoint, method="PUT", token=secret, body={"mode": mode})
    response = http(endpoint, token=secret)
    require(response.get("mode") in ("github", "poll"), "Unexpected callback routing mode")
    return response["mode"]


def import_state(token, args):
    verify_runtime_pins(args)
    require(scheduler_state() == "PAUSED", "Production scheduler must remain PAUSED during import")
    commit, state_json, digest = github_state()
    require(callback_control(args) == "poll", "Set callback routing to poll before importing state")
    existing, _ = read_document(token, absent_ok=True)
    if existing is not None:
        no_running_lease(existing)
        require(existing.get("enabled") is False and existing.get("last_outcome") == "imported"
                and existing.get("source_state_sha256") == digest and existing.get("state_json") == state_json,
                "Production state already exists and differs; it will not be overwritten")
        print("Matching disabled production state already imported; no write needed.")
        return
    patch_document(token, {
        "execution_mode": "live", "schema_version": 1, "enabled": False,
        "state_json": state_json, "source_repository": REPO, "source_commit": commit,
        "source_state_sha256": digest, "imported_at": datetime.now(timezone.utc),
        "lease_owner": None, "lease_until": None, "inflight_effect": None,
        "last_outcome": "imported",
    }, create=True)
    actual, _ = read_document(token)
    require(actual.get("state_json") == state_json and actual.get("enabled") is False,
            "Import readback did not match; inspect before retrying")
    print(f"Imported GitHub live state from {commit}; production remains disabled.")


def activate(token, args):
    verify_runtime_pins(args)
    require(scheduler_state() == "PAUSED", "Production scheduler must be PAUSED before activation")
    _, _, digest = github_state()
    require(callback_control(args) == "poll", "Callback routing must be poll")
    data, updated = read_document(token)
    no_running_lease(data)
    require(data.get("source_state_sha256") == digest and data.get("last_outcome") == "imported"
            and hashlib.sha256(data.get("state_json", "").encode()).hexdigest() == digest,
            "Production state no longer matches the final GitHub import")
    if not data.get("enabled"):
        patch_document(token, {"enabled": True}, update_time=updated)
    actual, _ = read_document(token)
    require(actual.get("enabled") is True, "Production activation readback failed")
    print("Production gate enabled. Scheduler remains PAUSED; no execution was started.")


def inspect(token):
    data, _ = read_document(token, absent_ok=True)
    if data is None:
        print("Production state has not been imported.")
        return
    state = validate_import_state(json.loads(data["state_json"]))
    marker = data.get("inflight_effect")
    summary = {"enabled": data.get("enabled"), "last_outcome": data.get("last_outcome"),
               "source_commit": data.get("source_commit"), "lease_present": bool(data.get("lease_owner")),
               "inflight_effect": ({"kind": marker.get("kind"), "key": marker.get("key")} if marker else None),
               "seen_count": len(state["seen"]), "last_run": state.get("last_run")}
    print(json.dumps(summary, indent=2))


def pause(token):
    gcloud("scheduler", "jobs", "pause", JOB, f"--location={REGION}")
    require(scheduler_state() == "PAUSED", "Production scheduler pause did not verify")
    data, updated = read_document(token)
    if data.get("enabled"):
        patch_document(token, {"enabled": False}, update_time=updated)
    print("Production scheduler and new effects paused. Drain running work; GitHub has not been re-enabled.")


def verify_execution_configuration(execution, image, versions_path, callback_url):
    """Validate the immutable execution's actual bindings, not only today's job.

    Cloud Run v1 Execution.spec is the same ExecutionSpec nested in a Job;
    v2 places taskCount, parallelism and the TaskTemplate on Execution itself.
    Preserve all task data so the preparation validator rejects overrides.
    """
    preparation = runpy.run_path(str(ROOT / "deploy/gcp/prepare-live.py"))
    try:
        require(bool(versions_path), "Supply the reviewed --versions JSON file")
        versions = json.loads(Path(versions_path).read_text())
        if "spec" in execution:
            metadata = dict(execution.get("metadata", {}))
            metadata["name"] = JOB
            equivalent_job = {"metadata": metadata, "spec": {"template": {"spec": execution["spec"]}}}
        else:
            equivalent_job = {
                "name": f"projects/{PROJECT}/locations/{REGION}/jobs/{JOB}",
                "template": {"taskCount": execution["taskCount"], "parallelism": execution["parallelism"],
                             "template": execution["template"]},
            }
        preparation["validate_inputs"](image, versions, callback_url)
        preparation["verify_job"](equivalent_job, image, versions, callback_url)
    except (KeyError, TypeError, AttributeError, OSError, ValueError, preparation["PreparationError"]):
        raise CutoverError("Successful execution configuration differs from the reviewed live deployment") from None


def resume(token, args):
    verify_runtime_pins(args)
    verify_github_stopped()
    require(callback_control(args) == "poll", "Callback routing must remain poll")
    data, _ = read_document(token)
    no_running_lease(data)
    require(data.get("enabled") is True and data.get("last_outcome") == "success",
            "A successful controlled live execution is required before scheduler activation")
    state = validate_import_state(json.loads(data["state_json"]))
    require(state["last_run"].get("errors") == 0 and args.confirmation == "CMA-CALLBACK-ALERT-VERIFIED",
            "Verify the complete live CMA/callback/alert evidence before resuming scheduling")
    jobs = gcloud("run", "jobs", "executions", "list", f"--job={JOB}", f"--region={REGION}",
                  "--limit=1", "--sort-by=~metadata.creationTimestamp")
    require(isinstance(jobs, list) and len(jobs) == 1, "Cannot verify the controlled live execution")
    execution_name = jobs[0].get("metadata", {}).get("name") or jobs[0].get("name", "").rsplit("/", 1)[-1]
    require(bool(re.fullmatch(r"flip-auto-live-[a-z0-9]+", execution_name)), "Unexpected live execution identity")
    execution = gcloud("run", "jobs", "executions", "describe", execution_name, f"--region={REGION}")
    status = execution.get("status", {})
    require(status.get("succeededCount") == 1 and status.get("failedCount", 0) == 0
            and any(c.get("type") == "Completed" and str(c.get("status")).lower() == "true"
                    for c in status.get("conditions", [])), "Last live execution did not complete successfully")
    verify_execution_configuration(execution, args.image, args.versions, args.callback_base_url)
    last = state["last_run"]
    require(last.get("execution_name") == execution_name,
            "Successful execution does not match the recorded production state")
    require(timestamp(status.get("startTime")) <= timestamp(last.get("started_at"))
            <= timestamp(last.get("finished_at")) <= timestamp(status.get("completionTime")),
            "Successful execution timestamps do not match production state")
    gcloud("scheduler", "jobs", "resume", JOB, f"--location={REGION}")
    require(scheduler_state() == "ENABLED", "Scheduler activation readback failed")
    print("Production scheduler enabled. GitHub remains disabled; shadow remains separate.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    for name in ("inspect", "callback-poll", "callback-github", "import-state", "activate", "pause", "resume-schedule"):
        actions.add_argument("--" + name, action="store_true")
    parser.add_argument("--image")
    parser.add_argument("--versions")
    parser.add_argument("--callback-base-url")
    parser.add_argument("--webhook-secret-version")
    parser.add_argument("--confirmation")
    args = parser.parse_args(argv)
    try:
        project = gcloud("projects", "describe", PROJECT)
        require(project.get("projectId") == PROJECT and str(project.get("projectNumber")) == PROJECT_NUMBER,
                "Expected the reviewed flip-auto project")
        if args.callback_poll:
            verify_github_stopped()
            require(callback_control(args, "poll") == "poll", "Callback poll readback failed")
            print("Callback routing reads poll. GitHub disable/drain is the ownership fence; KV propagation is asynchronous.")
            return 0
        token = call("gcloud", "auth", "print-access-token", f"--project={PROJECT}", "--quiet").strip()
        if args.callback_github:
            require(scheduler_state() == "PAUSED", "Pause GCP production before restoring callback dispatch")
            data, _ = read_document(token, absent_ok=True)
            if data:
                no_running_lease(data)
                require(data.get("enabled") is False, "Disable GCP effects before restoring callback dispatch")
            require(callback_control(args, "github") == "github", "Callback GitHub readback failed")
            print("Callback dispatch restored. GitHub was not enabled; reconcile/export live state before rollback.")
        elif args.inspect:
            inspect(token)
        elif args.import_state:
            import_state(token, args)
        elif args.activate:
            activate(token, args)
        elif args.pause:
            pause(token)
        elif args.resume_schedule:
            resume(token, args)
    except Exception as exc:
        message = str(exc) if isinstance(exc, CutoverError) else "Unexpected local or provider response; inspect before retrying"
        print("Cutover stopped: " + message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
