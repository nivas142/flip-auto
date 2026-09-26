#!/usr/bin/env python3
"""Create and run one separate replay job using the deployed, pinned image.

--check is offline. --apply creates the fixed new job, executes it, and reads
only its replay logs. Existing jobs are never updated, deleted, or re-executed.
--inspect EXECUTION only reads the status and logs of an existing replay.
No image build, IAM change, secret binding, or scheduler creation is performed.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


PROJECT = "flip-auto"
PROJECT_NUMBER = "941818435041"
REGION = "us-central1"
JOB = "flip-auto-cma-replay"
RUNTIME_SA = "flip-auto-shadow@flip-auto.iam.gserviceaccount.com"
IMAGE = (
    "us-central1-docker.pkg.dev/flip-auto/flip-auto/monitor@sha256:"
    "90854306a03c5856b9b7b7bac550b399db4e6b8fcdd9d9843a7d4cafadd4fce3"
)


class SetupError(RuntimeError):
    pass


class GcloudTimeout(SetupError):
    pass


class Gcloud:
    def run(self, *args, timeout=120, allow_failure=False):
        try:
            result = subprocess.run(
                ["gcloud", *args, f"--project={PROJECT}", "--quiet", "--format=json",
                 "--verbosity=error", "--no-log-http"],
                capture_output=True, text=True, check=False, timeout=timeout,
                env={**os.environ, "CLOUDSDK_CORE_DISABLE_PROMPTS": "1"},
            )
        except subprocess.TimeoutExpired as exc:
            if allow_failure:
                return None
            raise GcloudTimeout(f"gcloud {' '.join(args[:2])} timed out") from exc
        if result.returncode and not allow_failure:
            # Do not dump command arguments or response bodies into logs.
            raise SetupError(f"gcloud {' '.join(args[:3])} failed; exit {result.returncode}")
        if result.returncode:
            return None
        try:
            parsed = json.loads(result.stdout) if result.stdout.strip() else None
            return {} if allow_failure and parsed is None else parsed
        except json.JSONDecodeError as exc:
            raise SetupError("gcloud returned invalid JSON") from exc


def read_source():
    source = (Path(__file__).resolve().parents[2] / "scripts" / "gcp_cma_replay.py").read_bytes()
    if not source or len(source) > 40_000:
        raise SetupError("Reviewed adjacent replay script is missing or unexpectedly large")
    compile(source, "gcp_cma_replay.py", "exec")
    return source


def create_arguments(source):
    encoded = base64.b64encode(source).decode("ascii")
    code = f"import base64;exec(compile(base64.b64decode('{encoded}'),'gcp_cma_replay.py','exec'))"
    return [
        "run", "jobs", "create", JOB, f"--region={REGION}", f"--image={IMAGE}",
        f"--service-account={RUNTIME_SA}", "--tasks=1", "--parallelism=1",
        "--max-retries=0", "--task-timeout=900s", "--cpu=1", "--memory=1Gi",
        "--workdir=/app", "--command=python", f"--args=^|^-c|{code}",
        "--labels=app=flip-auto,mode=cma-replay",
    ]


def resource_name(resource):
    if not isinstance(resource, dict):
        raise SetupError("Unexpected Google resource response")
    name = resource.get("metadata", {}).get("name") or resource.get("name") or ""
    return str(name).rsplit("/", 1)[-1]


def deployed_image(resource):
    try:
        containers = resource["spec"]["template"]["spec"]["template"]["spec"]["containers"]
    except (KeyError, TypeError):
        try:
            containers = resource["template"]["template"]["containers"]
        except (KeyError, TypeError) as exc:
            raise SetupError("Cannot verify the deployed shadow image") from exc
    if len(containers) != 1:
        raise SetupError("Expected one deployed shadow container")
    return containers[0].get("image")


def verify_project(gcloud):
    project = gcloud.run("projects", "describe", PROJECT)
    if (not isinstance(project, dict) or project.get("projectId") != PROJECT
            or str(project.get("projectNumber")) != PROJECT_NUMBER
            or project.get("lifecycleState") != "ACTIVE"):
        raise SetupError("Expected active project flip-auto (941818435041)")


def validate_execution_name(execution_name):
    if not re.fullmatch(re.escape(JOB) + r"-[a-z0-9]+", execution_name):
        raise SetupError("Expected an exact flip-auto-cma-replay execution name")


def execution_image(resource):
    try:
        containers = resource["spec"]["template"]["spec"]["containers"]
    except (KeyError, TypeError):
        try:
            containers = resource["template"]["containers"]
        except (KeyError, TypeError) as exc:
            raise SetupError("Cannot verify the replay execution image") from exc
    if not isinstance(containers, list) or len(containers) != 1:
        raise SetupError("Expected one replay execution container")
    return containers[0].get("image")


def log_time_bounds(execution, now=None):
    status = execution.get("status") or {}
    metadata = execution.get("metadata") or {}

    def timestamp(value):
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("timezone missing")
            return parsed.astimezone(timezone.utc)
        except (TypeError, ValueError) as exc:
            raise SetupError("Cannot establish a bounded replay log time window") from exc

    start = timestamp(status.get("startTime") or metadata.get("creationTimestamp") or execution.get("createTime"))
    end_value = status.get("completionTime")
    end = timestamp(end_value) if end_value else (now or datetime.now(timezone.utc))
    if end < start:
        raise SetupError("Replay execution timestamps are inconsistent")
    return tuple(
        value.isoformat().replace("+00:00", "Z")
        for value in (start - timedelta(minutes=2), end + timedelta(minutes=2))
    )


def inspect_execution(execution_name, gcloud=None, output=None, sleeper=None,
                      *, project_verified=False, execute_wait_succeeded=True):
    """Read an exact execution; never create, run, update or delete resources."""
    validate_execution_name(execution_name)
    gcloud = gcloud or Gcloud()
    output = output or sys.stdout
    sleeper = sleeper or time.sleep
    recovery = f"python3 deploy/gcp/run-cma-replay.py --inspect {execution_name}"
    try:
        if not project_verified:
            verify_project(gcloud)
        execution = gcloud.run(
            "run", "jobs", "executions", "describe", execution_name, f"--region={REGION}",
        )
        if resource_name(execution) != execution_name:
            raise SetupError("Returned execution does not match the requested replay")
        job_label = (execution.get("metadata", {}).get("labels") or {}).get("run.googleapis.com/job")
        if job_label is not None and job_label != JOB:
            raise SetupError("Returned execution belongs to a different job")
        if execution_image(execution) != IMAGE:
            raise SetupError("Replay execution image differs from the reviewed replay image")
        status = execution.get("status") or {}
        completed = any(
            item.get("type") == "Completed" and str(item.get("status")).lower() == "true"
            for item in status.get("conditions", [])
        )
        task_succeeded = completed and int(status.get("succeededCount", 0)) == 1
        print(
            f"Execution {execution_name}: task {'succeeded' if task_succeeded else 'not confirmed successful'}; "
            f"succeededCount={status.get('succeededCount', 0)}. Replay baseline is not yet verified.",
            file=output, flush=True,
        )
        lower, upper = log_time_bounds(execution)
        log_filter = (
            'resource.type="cloud_run_job" '
            f'AND resource.labels.job_name="{JOB}" '
            f'AND resource.labels.location="{REGION}" '
            f'AND labels."run.googleapis.com/execution_name"="{execution_name}" '
            f'AND timestamp>="{lower}" AND timestamp<="{upper}" '
            'AND (textPayload:"[CMA_REPLAY]" OR textPayload:"[CMA_REPLAY_ERROR]")'
        )
        replay_results = []
        replay_errors = False
        for attempt in range(2):
            try:
                logs = gcloud.run(
                    "logging", "read", log_filter, "--limit=10", "--order=desc", timeout=35,
                )
            except GcloudTimeout:
                if attempt == 1:
                    raise SetupError("Log retrieval timed out; replay baseline remains unconfirmed")
                print("Log retrieval timed out; retrying the same bounded read once.", file=output, flush=True)
                sleeper(3)
                continue
            if not isinstance(logs, list):
                raise SetupError("Unexpected replay log response")
            if logs:
                for entry in reversed(logs):
                    text = str(entry.get("textPayload") or "")
                    if text.startswith("[CMA_REPLAY] "):
                        try:
                            result = json.loads(text.removeprefix("[CMA_REPLAY] "))
                        except json.JSONDecodeError as exc:
                            raise SetupError("Replay result log is not valid JSON") from exc
                        if not isinstance(result, dict):
                            raise SetupError("Replay result log is not an object")
                        replay_results.append(result)
                    if text.startswith("[CMA_REPLAY_ERROR] "):
                        replay_errors = True
                    if text.startswith(("[CMA_REPLAY] ", "[CMA_REPLAY_ERROR] ")):
                        print(text, file=output, flush=True)
                break
            if attempt == 0:
                sleeper(3)
        if not task_succeeded:
            raise SetupError(f"Replay execution {execution_name} did not complete successfully")
        if not execute_wait_succeeded:
            raise SetupError("Execute wait did not confirm completion; inspect the existing execution to confirm its result")
        if replay_errors:
            raise SetupError("Replay execution emitted an error; baseline is not verified")
        if len(replay_results) != 1:
            raise SetupError("No single replay result log is available yet; baseline remains unconfirmed")
        if replay_results[0].get("baseline_verified") is not True:
            raise SetupError("Replay calculation completed without confirming the pinned baseline")
        print(f"Replay execution {execution_name} completed. Normal shadow state was not used.", file=output, flush=True)
        return replay_results[0]
    except SetupError as exc:
        raise SetupError(f"{exc}. Read-only recovery: {recovery}") from exc


def apply(source, gcloud=None, output=None, sleeper=None):
    gcloud = gcloud or Gcloud()
    output = output or sys.stdout
    verify_project(gcloud)
    jobs = gcloud.run("run", "jobs", "list", f"--region={REGION}")
    if not isinstance(jobs, list):
        raise SetupError("Cannot verify that the replay job is absent")
    if any(resource_name(job) == JOB for job in jobs):
        raise SetupError("Replay job already exists; inspect it instead of rerunning setup")
    shadow = gcloud.run("run", "jobs", "describe", "flip-auto-shadow", f"--region={REGION}")
    if deployed_image(shadow) != IMAGE:
        raise SetupError("Deployed shadow image differs from the reviewed replay image")

    gcloud.run(*create_arguments(source), timeout=300)
    print("Created separate replay job without secrets or a schedule; starting one execution.", file=output, flush=True)
    executed = gcloud.run(
        "run", "jobs", "execute", JOB, f"--region={REGION}", "--wait",
        timeout=1200, allow_failure=True,
    )
    # This job was absent before this invocation and has no scheduler. Listing
    # its sole execution also provides scoped diagnostics when --wait failed.
    executions = gcloud.run(
        "run", "jobs", "executions", "list", f"--job={JOB}",
        f"--region={REGION}", "--limit=2",
    )
    if not isinstance(executions, list) or len(executions) != 1:
        raise SetupError("Expected exactly one replay execution; inspect the replay job")
    execution_name = resource_name(executions[0])
    return inspect_execution(
        execution_name, gcloud, output, sleeper, project_verified=True,
        execute_wait_succeeded=executed is not None,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="offline validation only (default)")
    group.add_argument("--apply", action="store_true", help="create and execute the separate replay job")
    group.add_argument("--inspect", metavar="EXECUTION", help="read only an existing replay execution's status and logs")
    options = parser.parse_args(argv)
    try:
        print(f"Target: {PROJECT}/{REGION}/{JOB}; image {IMAGE}")
        if options.inspect:
            inspect_execution(options.inspect)
        else:
            source = read_source()
            print(f"Replay source SHA256: {hashlib.sha256(source).hexdigest()}")
            if options.apply:
                apply(source)
            else:
                create_arguments(source)
                print("Offline validation passed. No cloud resources or secrets accessed.")
        return 0
    except Exception as exc:
        detail = str(exc) if isinstance(exc, SetupError) else type(exc).__name__
        print(f"Replay setup stopped: {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
