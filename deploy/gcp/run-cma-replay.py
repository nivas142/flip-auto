#!/usr/bin/env python3
"""Create and run one separate replay job using the deployed, pinned image.

--check is offline. --apply creates the fixed new job, executes it, and reads
only its replay logs. Existing jobs are never updated, deleted, or re-executed.
No image build, IAM change, secret binding, or scheduler creation is performed.
"""
from __future__ import annotations

import argparse
import base64
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
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "gcp_cma_replay.py"


class SetupError(RuntimeError):
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
            raise SetupError(f"gcloud {' '.join(args[:3])} timed out") from exc
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
    source = SCRIPT.read_bytes()
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


def apply(source, gcloud=None, output=None, sleeper=None):
    gcloud = gcloud or Gcloud()
    output = output or sys.stdout
    sleeper = sleeper or time.sleep
    project = gcloud.run("projects", "describe", PROJECT)
    if (not isinstance(project, dict) or project.get("projectId") != PROJECT
            or str(project.get("projectNumber")) != PROJECT_NUMBER
            or project.get("lifecycleState") != "ACTIVE"):
        raise SetupError("Expected active project flip-auto (941818435041)")
    jobs = gcloud.run("run", "jobs", "list", f"--region={REGION}")
    if not isinstance(jobs, list):
        raise SetupError("Cannot verify that the replay job is absent")
    if any(resource_name(job) == JOB for job in jobs):
        raise SetupError("Replay job already exists; inspect it instead of rerunning setup")
    shadow = gcloud.run("run", "jobs", "describe", "flip-auto-shadow", f"--region={REGION}")
    if deployed_image(shadow) != IMAGE:
        raise SetupError("Deployed shadow image differs from the reviewed replay image")

    gcloud.run(*create_arguments(source), timeout=300)
    print("Created separate replay job without secrets or a schedule; starting one execution.", file=output)
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
    if not re.fullmatch(re.escape(JOB) + r"-[a-z0-9]+", execution_name):
        raise SetupError("Unexpected replay execution name")
    execution = gcloud.run(
        "run", "jobs", "executions", "describe", execution_name, f"--region={REGION}",
    )
    log_filter = (
        'resource.type="cloud_run_job" '
        f'AND resource.labels.job_name="{JOB}" '
        f'AND resource.labels.location="{REGION}" '
        f'AND labels."run.googleapis.com/execution_name"="{execution_name}" '
        'AND (textPayload:"[CMA_REPLAY]" OR textPayload:"[CMA_REPLAY_ERROR]")'
    )
    replay_results = []
    for attempt in range(4):
        logs = gcloud.run("logging", "read", log_filter, "--limit=10", "--order=asc")
        if not isinstance(logs, list):
            raise SetupError("Unexpected replay log response")
        if logs:
            for entry in logs:
                text = str(entry.get("textPayload") or "")
                if text.startswith("[CMA_REPLAY] "):
                    replay_results.append(json.loads(text.removeprefix("[CMA_REPLAY] ")))
                if text.startswith(("[CMA_REPLAY] ", "[CMA_REPLAY_ERROR] ")):
                    print(text, file=output)
            break
        if attempt < 3:
            sleeper(5)
    status = execution.get("status") or {}
    completed = any(
        item.get("type") == "Completed" and str(item.get("status")).lower() == "true"
        for item in status.get("conditions", [])
    )
    if executed is None or not completed or int(status.get("succeededCount", 0)) != 1:
        raise SetupError(f"Replay execution {execution_name} did not complete successfully")
    if len(replay_results) != 1:
        raise SetupError(f"Replay execution {execution_name} has no single result log yet; inspect its logs")
    if replay_results[0].get("baseline_verified") is not True:
        raise SetupError("Replay calculation completed without confirming the pinned baseline")
    print(f"Replay execution {execution_name} completed. Normal shadow state was not used.", file=output)
    return replay_results[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="offline validation only (default)")
    group.add_argument("--apply", action="store_true", help="create and execute the separate replay job")
    options = parser.parse_args(argv)
    try:
        source = read_source()
        print(f"Replay source SHA256: {hashlib.sha256(source).hexdigest()}")
        print(f"Target: {PROJECT}/{REGION}/{JOB}; image {IMAGE}")
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
