#!/usr/bin/env python3
"""Add one fixed set of existing GitHub secret values to shadow secrets.

The dedicated direct WIF identity needs only secretVersionAdder on the selected
resources. It cannot list or access versions to deduplicate. Every rerun adds new
versions; a failed request may already have committed. Never retry automatically.
No GitHub/Cloudflare secrets or runtime deployments are changed by this helper.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from collections.abc import MutableMapping
from typing import TextIO


PROJECT_ID = "flip-auto"
PROJECT_NUMBER = "941818435041"
SECRET_MAPPING = (
    ("EMAIL_USERNAME", "flip-auto-email-username"),
    ("EMAIL_APP_PASSWORD", "flip-auto-email-app-password"),
    ("CLOUD_CMA_WEBHOOK_SECRET", "flip-auto-cma-webhook-secret"),
)

SECRET_PROFILES = {
    "core": SECRET_MAPPING,
    "zoho": (
        ("ZOHO_EMAIL_USERNAME", "flip-auto-zoho-email-username"),
        ("ZOHO_EMAIL_APP_PASSWORD", "flip-auto-zoho-email-app-password"),
    ),
}

# Do not inherit arbitrary CLOUDSDK settings (endpoint overrides, tracing,
# impersonation, alternate projects) or shell/Python debugging hooks. gcloud
# authenticates directly from the ephemeral file created/cleaned by auth@v3.
GCLOUD_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE",
    "GOOGLE_APPLICATION_CREDENTIALS",
)


class TransferError(Exception):
    """Contains only messages built from fixed identifiers, never subprocess data."""


def _take_payloads(environ: MutableMapping[str, str], mapping=SECRET_MAPPING) -> dict[str, bytes]:
    # Remove every source from the process environment, including on validation
    # failure, and validate the complete set before any external command runs.
    values = {name: environ.pop(name, "") for name, _ in mapping}
    missing = [name for name, value in values.items() if not value.strip()]
    if missing:
        raise TransferError("Missing required GitHub secrets: " + ", ".join(missing))
    try:
        # Preserve whitespace and line endings exactly; strip() is validation only.
        return {name: value.encode("utf-8") for name, value in values.items()}
    except UnicodeError:
        raise TransferError("A required secret is not valid UTF-8; nothing was uploaded.") from None


def _gcloud_environment(environ: MutableMapping[str, str], config_dir: str) -> dict[str, str]:
    child_env = {name: environ[name] for name in GCLOUD_ENV_ALLOWLIST if name in environ}
    child_env.update({
        "CLOUDSDK_CONFIG": config_dir,
        "CLOUDSDK_CORE_PROJECT": PROJECT_ID,
        "CLOUDSDK_CORE_DISABLE_FILE_LOGGING": "true",
        "CLOUDSDK_CORE_LOG_HTTP": "false",
        "CLOUDSDK_CORE_VERBOSITY": "error",
        "CLOUDSDK_CORE_DISABLE_USAGE_REPORTING": "true",
        "CLOUDSDK_CORE_DISABLE_PROMPTS": "true",
        "CLOUDSDK_CORE_UNIVERSE_DOMAIN": "googleapis.com",
    })
    return child_env


def _verified_version(raw: bytes, secret_id: str) -> str:
    try:
        resource = raw.decode("ascii").strip()
    except UnicodeError:
        raise TransferError(f"Unverified result for {secret_id}; a version may already exist.") from None
    # The API may canonicalize the project ID to its immutable project number.
    pattern = (
        rf"projects/(?:{re.escape(PROJECT_ID)}|{PROJECT_NUMBER})/secrets/"
        rf"{re.escape(secret_id)}/versions/[1-9][0-9]*"
    )
    if not re.fullmatch(pattern, resource):
        raise TransferError(f"Unverified result for {secret_id}; a version may already exist.")
    return resource


def transfer_secrets(environ: MutableMapping[str, str], output: TextIO, profile="core") -> tuple[str, ...]:
    if profile not in SECRET_PROFILES:
        raise TransferError("Unknown transfer profile; nothing was uploaded.")
    mapping = SECRET_PROFILES[profile]
    payloads = _take_payloads(environ, mapping)
    versions = []
    # Isolate and remove any CLI state/credential cache as soon as the transfer
    # ends. Payloads only pass through pipes; no payload is ever written to disk.
    with tempfile.TemporaryDirectory(prefix="flip-auto-secret-transfer-") as config_dir:
        child_env = _gcloud_environment(environ, config_dir)
        for source_name, secret_id in mapping:
            command = [
                "gcloud", "secrets", "versions", "add", secret_id,
                f"--project={PROJECT_ID}", "--data-file=-", "--format=value(name)",
                "--quiet", "--verbosity=error", "--no-log-http",
            ]
            try:
                result = subprocess.run(
                    command,
                    input=payloads.pop(source_name),
                    env=child_env,
                    capture_output=True,
                    check=False,
                    timeout=60,
                )
            except (OSError, subprocess.SubprocessError):
                # Exceptions can include commands, payloads or remote output.
                # Do not print or chain them, including TimeoutExpired.output.
                raise TransferError(
                    f"Transfer stopped at {secret_id}; the request may have completed. "
                    "No retry was attempted."
                ) from None
            if result.returncode != 0:
                raise TransferError(
                    f"Transfer stopped at {secret_id}; the request may have completed. "
                    "No retry was attempted."
                )
            version = _verified_version(result.stdout, secret_id)
            versions.append(version)
            # Only validated resource IDs reach the log, even on partial success.
            print(version, file=output, flush=True)
    return tuple(versions)


def check_zoho_config(environ: MutableMapping[str, str], output: TextIO) -> None:
    """Check effective production options without printing their secret values.

    Production inherits Gmail's window if Zoho's setting is absent. The shadow
    image's reviewed defaults are imap.zoho.com / Off-Market-Deals / 48 hours.
    This preflight runs before authentication or any credential upload.
    """
    names = ("ZOHO_IMAP_HOST", "ZOHO_FOLDER", "ZOHO_LOOKBACK_HOURS", "EMAIL_LOOKBACK_HOURS")
    settings = {name: environ.pop(name, "").strip() for name in names}
    try:
        hours_match = int(settings["ZOHO_LOOKBACK_HOURS"] or settings["EMAIL_LOOKBACK_HOURS"] or "48") == 48
    except (ValueError, TypeError):
        hours_match = False
    matches = {
        "ZOHO_HOST_MATCH": (settings["ZOHO_IMAP_HOST"] or "imap.zoho.com") == "imap.zoho.com",
        "ZOHO_FOLDER_MATCH": (settings["ZOHO_FOLDER"] or "Off-Market-Deals") == "Off-Market-Deals",
        "ZOHO_LOOKBACK_MATCH": hours_match,
    }
    for name, matched in matches.items():
        print(f"{name}={str(matched).lower()}", file=output)
    if not all(matches.values()):
        raise TransferError("Zoho configuration differs from reviewed shadow defaults; nothing was uploaded.")


def main() -> int:
    args = sys.argv[1:]
    # Deliberately do not echo rejected arguments: they could contain payloads.
    if args not in ([], ["--profile", "core"], ["--profile", "zoho"], ["--check-zoho-config"]):
        print("Only a fixed core/zoho profile or Zoho configuration check is accepted.", file=sys.stderr)
        return 2
    try:
        if args == ["--check-zoho-config"]:
            check_zoho_config(os.environ, sys.stdout)
        elif args:
            transfer_secrets(os.environ, sys.stdout, profile=args[1])
        else:
            # Preserve the original callable path as well as CLI behavior.
            transfer_secrets(os.environ, sys.stdout)
    except TransferError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        # A traceback or unexpected exception message could reveal a payload.
        print("Transfer stopped unexpectedly; do not retry without checking existing version metadata.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
