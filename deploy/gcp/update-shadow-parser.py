#!/usr/bin/env python3
"""One-time parser-only shadow image promotion; no executions or schedule changes."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

PROJECT = "flip-auto"
PROJECT_NUMBER = "941818435041"
REGION = "us-central1"
JOB = "flip-auto-shadow"
SERVICE_ACCOUNT = "flip-auto-shadow@flip-auto.iam.gserviceaccount.com"
REPOSITORY = "us-central1-docker.pkg.dev/flip-auto/flip-auto/monitor"
BASE_IMAGE = REPOSITORY + "@sha256:90854306a03c5856b9b7b7bac550b399db4e6b8fcdd9d9843a7d4cafadd4fce3"
PARSER_SHA = "c8d05718135ed6656830ae2b91c6a07ef4e4cebc357cab3f02cc424cc3886e65"
REPLAY_SHA = "c8ec40b0412b369a2e43d55bab76f921cc10bb605997aa472d07561a6fb29b63"
RESULT_SHA = "7907073ef91c53d371fd5bf0a29b9932ec3829c6598ef2fb5211b98638b2ad7e"


class RolloutError(RuntimeError):
    """Only fixed, non-secret messages are emitted to the operator."""


class Commands:
    def run(self, *args, timeout=120):
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RolloutError(f"{args[0]} {args[1]} could not complete") from exc
        if result.returncode:
            if args[:2] == ("docker", "run"):
                # Only the fixed reviewed replay's diagnostic records, never
                # arbitrary container output or Docker credential errors.
                records = [line for line in (result.stdout + "\n" + result.stderr).splitlines()
                           if line.startswith(("[CMA_REPLAY_DIAGNOSTIC] ", "[CMA_REPLAY_ERROR] "))]
                for line in records[:4]:
                    print(line[:2000], file=sys.stderr)
            raise RolloutError(f"{args[0]} {args[1]} failed; exit {result.returncode}")
        return result.stdout

    def gcloud(self, *args):
        output = self.run("gcloud", *args, f"--project={PROJECT}", "--quiet", "--format=json",
                          "--verbosity=error", "--no-log-http", timeout=300)
        try:
            return json.loads(output)
        except json.JSONDecodeError as exc:
            raise RolloutError("Unexpected Google response") from exc


def reviewed_files(root=None):
    root = root or Path(__file__).resolve().parents[2]
    paths = (root / "cloud_cma.py", root / "scripts/gcp_cma_replay.py")
    for path, expected in zip(paths, (PARSER_SHA, REPLAY_SHA)):
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RolloutError("Reviewed parser or replay source is missing or changed")
    return paths


def job_configuration(job, expected_image):
    """Compare the whole task configuration, excluding only the intended image change."""
    try:
        name = job.get("metadata", {}).get("name") or job.get("name", "")
        if name.rsplit("/", 1)[-1] != JOB:
            raise RolloutError("Unexpected shadow job")
        if "spec" in job:
            execution = job["spec"]["template"]["spec"]
            task = copy.deepcopy(execution["template"]["spec"])
            account = task.get("serviceAccountName")
        else:
            execution = job["template"]
            task = copy.deepcopy(execution["template"])
            account = task.get("serviceAccount")
        containers = task["containers"]
        if len(containers) != 1 or account != SERVICE_ACCOUNT:
            raise RolloutError("Unexpected shadow container or service account")
        container = containers[0]
        if container.get("image") != expected_image:
            raise RolloutError("Shadow image is not the expected digest; no overwrite attempted")
        modes = [item for item in container.get("env", []) if item.get("name") == "FLIP_AUTO_EXECUTION_MODE"]
        if modes != [{"name": "FLIP_AUTO_EXECUTION_MODE", "value": "shadow"}]:
            raise RolloutError("Explicit shadow mode is required")
        if container.get("command") or container.get("args"):
            raise RolloutError("Expected the image's default shadow entrypoint")
        if (execution.get("taskCount") != 1 or execution.get("parallelism") != 1
                or task.get("maxRetries") != 0):
            raise RolloutError("Expected one shadow task, parallelism one and zero retries")
        container["image"] = "<reviewed-image>"
        return {"task": task, "taskCount": 1, "parallelism": 1}
    except (KeyError, TypeError, AttributeError) as exc:
        raise RolloutError("Unexpected shadow job configuration") from exc


def verify_replay(output):
    try:
        results = [json.loads(line.removeprefix("[CMA_REPLAY] "))
                   for line in output.splitlines() if line.startswith("[CMA_REPLAY] ")]
        if len(results) != 1:
            raise ValueError("missing result")
        result = results[0]
        if (result.get("baseline_verified") is not True or result.get("module_profile") != "current-parser"
                or result.get("result_sha256") != RESULT_SHA
                or result.get("module_sha256", {}).get("cloud_cma") != PARSER_SHA
                or result.get("side_effects") != {"alerts_sent": 0, "cma_requests": 0, "persistent_state_writes": 0}):
            raise ValueError("baseline mismatch")
    except (ValueError, TypeError, AttributeError) as exc:
        raise RolloutError("Candidate replay did not verify the reviewed baseline") from exc
    return result


def apply(root=None, commands=None, output=None):
    parser, replay = reviewed_files(root)
    commands, output = commands or Commands(), output or sys.stdout
    project = commands.gcloud("projects", "describe", PROJECT)
    if (project.get("projectId") != PROJECT or str(project.get("projectNumber")) != PROJECT_NUMBER
            or project.get("lifecycleState") != "ACTIVE"):
        raise RolloutError("Expected active project flip-auto (941818435041)")
    describe = ("run", "jobs", "describe", JOB, f"--region={REGION}")
    original = job_configuration(commands.gcloud(*describe), BASE_IMAGE)
    tag = REPOSITORY + ":parser-v3-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
    with tempfile.TemporaryDirectory(prefix="flip-auto-parser-rollout-") as directory:
        context = Path(directory)
        shutil.copyfile(parser, context / "cloud_cma.py")
        (context / "cloud_cma.py").chmod(0o644)
        (context / "Dockerfile").write_text(
            f"FROM {BASE_IMAGE}\nCOPY --chown=10001:10001 cloud_cma.py /app/cloud_cma.py\n")
        print("Building parser-only image from the reviewed base; dependencies are unchanged.", file=output, flush=True)
        commands.run("docker", "build", "--platform=linux/amd64", "--tag", tag, str(context), timeout=600)
        replay_mount = f"type=bind,src={replay.resolve()},dst=/app/gcp_cma_replay.py,readonly"
        result = commands.run("docker", "run", "--rm", "--read-only", "--cap-drop=ALL",
                              "--security-opt=no-new-privileges", "--workdir=/app", "--entrypoint=python",
                              "--mount", replay_mount, tag, "/app/gcp_cma_replay.py", timeout=600)
        verified = verify_replay(result)
        print(f"Replay baseline verified: {RESULT_SHA}; pypdf={verified.get('pypdf_version', 'unknown')}; "
              f"Python={verified.get('python_version', 'unknown')}.", file=output, flush=True)
    print("Baked-image replay verified. Publishing the candidate image.", file=output, flush=True)
    commands.run("docker", "push", tag, timeout=600)
    try:
        digests = json.loads(commands.run("docker", "image", "inspect", tag, "--format={{json .RepoDigests}}"))
        matches = [value for value in digests if isinstance(value, str)
                   and re.fullmatch(re.escape(REPOSITORY) + r"@sha256:[a-f0-9]{64}", value)]
        if len(matches) != 1 or matches[0] == BASE_IMAGE:
            raise ValueError("unexpected digest")
        image = matches[0]
    except (ValueError, TypeError) as exc:
        raise RolloutError("Cannot verify the published candidate digest") from exc
    if job_configuration(commands.gcloud(*describe), BASE_IMAGE) != original:
        raise RolloutError("Shadow configuration changed during verification; promotion stopped")
    print(f"Previous shadow image (rollback): {BASE_IMAGE}", file=output, flush=True)
    print("Requesting image-only shadow update; if interrupted, inspect the job before retrying.", file=output, flush=True)
    commands.gcloud("run", "jobs", "update", JOB, f"--region={REGION}", f"--image={image}")
    if job_configuration(commands.gcloud(*describe), image) != original:
        raise RolloutError("Image update attempted; shadow configuration readback did not verify")
    print(f"Verified shadow image: {image}", file=output)
    print("Only the shadow job image was updated. No execution was started; scheduler, IAM and secrets were not changed.", file=output)
    return image


def main(argv=None):
    argument_parser = argparse.ArgumentParser(description=__doc__)
    action = argument_parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true")
    action.add_argument("--apply", action="store_true")
    args = argument_parser.parse_args(argv)
    try:
        if args.check:
            reviewed_files()
            print("Offline source verification passed. No cloud resources or credentials accessed.")
        else:
            apply()
    except RolloutError as exc:
        print(f"Parser rollout stopped: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print("Parser rollout stopped: unexpected local or provider response; inspect the shadow job before retrying.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
