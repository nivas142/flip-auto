#!/usr/bin/env python3
"""Promote reviewed parser v4 to the PAUSED live job without executing it."""
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
JOB = "flip-auto-live"
SERVICE_ACCOUNT = "flip-auto-live@flip-auto.iam.gserviceaccount.com"
REPOSITORY = "us-central1-docker.pkg.dev/flip-auto/flip-auto/monitor"
BASE_IMAGE = REPOSITORY + "@sha256:8470acdbb7e4d23ffb8eb54e6b3b6518709213a2ab200f72064257ff45b59a01"
PARSER_SHA = "d262aae235126c60ede4599936fbb74fdbf7f9dddc884ba2054b05a646167415"
MONITOR_SHA = "e32bb693bf144d3f32f504449c6d9837c66c3e7bb5aeb2092e438af03a718d1f"
DEAL_SCREENING_SHA = "064c2ee8d4bf3e8b023e29e00c03575c0a67e68c166bf7e74b75e30ba6377186"
LIVE_RUNTIME_SHA = "796803982efb435801cfde4c6000a50f8c050aaafb29132f6f05ccee12dbe511"
RECOVERY_SHA = "f42f273cc12fe6e063d0c0452b010a400a64e9ce016e2e069cd00409e0f65c2f"
RECOVERY_TEXT_SHA = "d2a1c3ea7a48a1d40a1fa327ff5c1fdf8430503b731d248ac0f6d12c3a63e69e"
RECOVERY_RESULT_SHA = "fe62ead23bfb74aacd0349b08306da7743697dc479da95c2a5bfa68199afaf0d"


class RolloutError(RuntimeError):
    """Only fixed, non-sensitive messages are emitted to the operator."""


class Commands:
    def run(self, *args: str, timeout: int = 120) -> str:
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RolloutError(f"{args[0]} {args[1]} could not complete") from exc
        if result.returncode:
            records = [
                line
                for line in (result.stdout + "\n" + result.stderr).splitlines()
                if line.startswith(("[CMA_RECOVERY] ", "[CMA_RECOVERY_ERROR] "))
            ]
            for line in records[:3]:
                print(line[:2000], file=sys.stderr)
            raise RolloutError(f"{args[0]} {args[1]} failed; exit {result.returncode}")
        return result.stdout

    def gcloud(self, *args: str) -> object:
        output = self.run(
            "gcloud",
            *args,
            f"--project={PROJECT}",
            "--quiet",
            "--format=json",
            "--verbosity=error",
            "--no-log-http",
            timeout=600,
        )
        try:
            return json.loads(output) if output.strip() else {}
        except json.JSONDecodeError as exc:
            raise RolloutError("Unexpected Google response") from exc


def reviewed_files(root: Path | None = None) -> tuple[Path, Path, Path, Path, Path]:
    root = root or Path(__file__).resolve().parents[2]
    paths = (
        root / "cloud_cma.py",
        root / "monitor.py",
        root / "deal_screening.py",
        root / "gcp_live_runtime.py",
        root / "scripts/gcp_cma_recovery_verify.py",
    )
    expected = (PARSER_SHA, MONITOR_SHA, DEAL_SCREENING_SHA, LIVE_RUNTIME_SHA, RECOVERY_SHA)
    for path, digest in zip(paths, expected):
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise RolloutError("Reviewed parser recovery source is missing or changed")
    return paths


def verify_recovery_text(path: Path) -> None:
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise RolloutError("Recovery text is missing or too large")
    if hashlib.sha256(path.read_bytes()).hexdigest() != RECOVERY_TEXT_SHA:
        raise RolloutError("Recovery text does not match the retained report")


def job_configuration(job: object, expected_image: str) -> dict[str, object]:
    """Return the whole task configuration with only its image normalized."""
    try:
        if not isinstance(job, dict):
            raise RolloutError("Unexpected live job")
        v1 = "spec" in job
        name = job.get("metadata", {}).get("name") if v1 else job.get("name", "")
        if str(name).rsplit("/", 1)[-1] != JOB:
            raise RolloutError("Unexpected live job")
        execution = job["spec"]["template"]["spec"] if v1 else job["template"]
        task = copy.deepcopy(execution["template"]["spec"] if v1 else execution["template"])
        account = task.get("serviceAccountName" if v1 else "serviceAccount")
        containers = task["containers"]
        if len(containers) != 1 or account != SERVICE_ACCOUNT:
            raise RolloutError("Unexpected live container or service account")
        container = containers[0]
        if container.get("image") != expected_image:
            raise RolloutError("Live image is not the expected digest; no overwrite attempted")
        modes = [item for item in container.get("env", []) if item.get("name") == "FLIP_AUTO_EXECUTION_MODE"]
        if modes != [{"name": "FLIP_AUTO_EXECUTION_MODE", "value": "live"}]:
            raise RolloutError("Explicit live mode is required")
        if container.get("command") != ["python"] or container.get("args") != ["gcp_live_runtime.py"]:
            raise RolloutError("Expected the reviewed live entrypoint")
        timeout = task.get("timeoutSeconds") if v1 else task.get("timeout")
        if (
            execution.get("taskCount") != 1
            or execution.get("parallelism") != 1
            or task.get("maxRetries") != 0
            or timeout not in (900, "900", "900s")
        ):
            raise RolloutError("Unexpected live task execution policy")
        container["image"] = "<reviewed-image>"
        return {"task": task, "taskCount": 1, "parallelism": 1}
    except (KeyError, TypeError, AttributeError) as exc:
        raise RolloutError("Unexpected live job configuration") from exc


def verify_paused_scheduler(scheduler: object) -> None:
    try:
        if not isinstance(scheduler, dict) or scheduler.get("state") != "PAUSED":
            raise RolloutError("Live scheduler must remain PAUSED")
        target = scheduler["httpTarget"]
        expected_suffix = f"/projects/{PROJECT}/locations/{REGION}/jobs/{JOB}:run"
        if target.get("httpMethod") != "POST" or not str(target.get("uri", "")).endswith(expected_suffix):
            raise RolloutError("Unexpected live scheduler target")
    except (KeyError, TypeError, AttributeError) as exc:
        raise RolloutError("Unexpected live scheduler configuration") from exc


def verify_no_active_executions(executions: object) -> None:
    if not isinstance(executions, list):
        raise RolloutError("Unexpected live execution listing")
    for execution in executions:
        status = execution.get("status", {}) if isinstance(execution, dict) else {}
        completed = bool(status.get("completionTime")) or any(
            condition.get("type") == "Completed" and condition.get("status") == "True"
            for condition in status.get("conditions", [])
            if isinstance(condition, dict)
        )
        if not completed:
            raise RolloutError("A live execution may still be active; promotion stopped")


def verify_recovery_output(output: str) -> dict[str, object]:
    try:
        records = [
            json.loads(line.removeprefix("[CMA_RECOVERY] "))
            for line in output.splitlines()
            if line.startswith("[CMA_RECOVERY] ")
        ]
        if len(records) != 1:
            raise ValueError("missing result")
        result = records[0]
        if (
            result.get("verified") is not True
            or result.get("parser_version") != 4
            or result.get("result_sha256") != RECOVERY_RESULT_SHA
            or result.get("page_count") != 86
            or result.get("parsed_closed_comps") != 7
            or result.get("eligible_closed_comps") != 6
            or result.get("side_effects")
            != {"alerts_sent": 0, "cma_requests": 0, "persistent_state_writes": 0}
        ):
            raise ValueError("baseline mismatch")
        return result
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        raise RolloutError("Candidate recovery verification did not match the reviewed baseline") from exc


def apply(
    recovery_text: Path,
    *,
    root: Path | None = None,
    commands: Commands | None = None,
    output: object | None = None,
) -> str:
    parser, monitor, deal_screening, live_runtime, verifier = reviewed_files(root)
    verify_recovery_text(recovery_text)
    commands = commands or Commands()
    output = output or sys.stdout
    project = commands.gcloud("projects", "describe", PROJECT)
    if (
        not isinstance(project, dict)
        or project.get("projectId") != PROJECT
        or str(project.get("projectNumber")) != PROJECT_NUMBER
        or project.get("lifecycleState") != "ACTIVE"
    ):
        raise RolloutError("Expected active project flip-auto (941818435041)")

    describe = ("run", "jobs", "describe", JOB, f"--region={REGION}")
    scheduler_describe = ("scheduler", "jobs", "describe", JOB, f"--location={REGION}")
    original = job_configuration(commands.gcloud(*describe), BASE_IMAGE)
    verify_paused_scheduler(commands.gcloud(*scheduler_describe))
    list_executions = ("run", "jobs", "executions", "list", f"--job={JOB}", f"--region={REGION}")
    verify_no_active_executions(commands.gcloud(*list_executions))

    tag = (
        REPOSITORY
        + ":live-parser-v4-"
        + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        + "-"
        + uuid.uuid4().hex[:8]
    )
    with tempfile.TemporaryDirectory(prefix="flip-auto-live-parser-") as directory:
        context = Path(directory)
        for source in (parser, monitor, deal_screening, live_runtime):
            shutil.copyfile(source, context / source.name)
            (context / source.name).chmod(0o644)
        (context / "scripts").mkdir()
        shutil.copyfile(verifier, context / "scripts" / verifier.name)
        (context / "scripts" / verifier.name).chmod(0o644)
        (context / "Dockerfile").write_text(
            f"FROM {BASE_IMAGE}\n"
            "COPY --chown=10001:10001 cloud_cma.py /app/cloud_cma.py\n"
            "COPY --chown=10001:10001 monitor.py /app/monitor.py\n"
            "COPY --chown=10001:10001 deal_screening.py /app/deal_screening.py\n"
            "COPY --chown=10001:10001 gcp_live_runtime.py /app/gcp_live_runtime.py\n"
            "COPY --chown=10001:10001 scripts/gcp_cma_recovery_verify.py "
            "/app/scripts/gcp_cma_recovery_verify.py\n",
            encoding="utf-8",
        )
        print("Building the reviewed live parser image; runtime dependencies are unchanged.", file=output, flush=True)
        commands.run("docker", "build", "--platform=linux/amd64", "--tag", tag, str(context), timeout=900)
        recovery_mount = (
            f"type=bind,src={recovery_text.resolve()},"
            "dst=/recovery/cloud-cma-bmlzs.txt,readonly"
        )
        result = commands.run(
            "docker",
            "run",
            "--rm",
            "--read-only",
            "--network=none",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--workdir=/app",
            "--entrypoint=python",
            "--mount",
            recovery_mount,
            tag,
            "/app/scripts/gcp_cma_recovery_verify.py",
            "/recovery/cloud-cma-bmlzs.txt",
            timeout=900,
        )
        verified = verify_recovery_output(result)
        print(
            f"Retained report verified: {verified['parsed_closed_comps']} parsed and "
            f"{verified['eligible_closed_comps']} eligible closed comps.",
            file=output,
            flush=True,
        )

    print("Publishing the verified candidate image.", file=output, flush=True)
    commands.run("docker", "push", tag, timeout=900)
    try:
        digests = json.loads(commands.run("docker", "image", "inspect", tag, "--format={{json .RepoDigests}}"))
        matches = [
            value
            for value in digests
            if isinstance(value, str)
            and re.fullmatch(re.escape(REPOSITORY) + r"@sha256:[a-f0-9]{64}", value)
        ]
        if len(matches) != 1 or matches[0] == BASE_IMAGE:
            raise ValueError("unexpected digest")
        image = matches[0]
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RolloutError("Cannot verify the published candidate digest") from exc

    if job_configuration(commands.gcloud(*describe), BASE_IMAGE) != original:
        raise RolloutError("Live configuration changed during verification; promotion stopped")
    verify_paused_scheduler(commands.gcloud(*scheduler_describe))
    verify_no_active_executions(commands.gcloud(*list_executions))
    print(f"Previous live image (rollback): {BASE_IMAGE}", file=output, flush=True)
    print("Requesting an image-only live job update; no execution will start.", file=output, flush=True)
    commands.gcloud("run", "jobs", "update", JOB, f"--region={REGION}", f"--image={image}")
    if job_configuration(commands.gcloud(*describe), image) != original:
        raise RolloutError("Image update attempted; live configuration readback did not verify")
    verify_paused_scheduler(commands.gcloud(*scheduler_describe))
    print(f"Verified live image: {image}", file=output)
    print("Live scheduler remains PAUSED. No job execution, secret change, or IAM change was requested.", file=output)
    return image


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true")
    action.add_argument("--apply", action="store_true")
    parser.add_argument("--recovery-text", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        reviewed_files()
        verify_recovery_text(args.recovery_text)
        if args.check:
            print("Offline parser-v4 source and retained recovery text verification passed.")
            print("No cloud resources, credentials, secret values, or report contents were accessed.")
        else:
            apply(args.recovery_text)
    except RolloutError as exc:
        print(f"Live parser rollout stopped: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print("Live parser rollout stopped: unexpected local or provider response; inspect the live job before retrying.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
