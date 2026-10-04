#!/usr/bin/env python3
"""Prepare isolated production resources; never execute a job or resume a schedule.

Only secret metadata is read. The default container entrypoint remains shadow;
this create-only deployment explicitly selects the separately gated live runner.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit

PROJECT = "flip-auto"
PROJECT_NUMBER = "941818435041"
REGION = "us-central1"
JOB = DATABASE = "flip-auto-live"
COLLECTION = "flip_auto_live_state"
RUNTIME_ACCOUNT = f"{JOB}@{PROJECT}.iam.gserviceaccount.com"
SCHEDULER_ACCOUNT = f"{JOB}-scheduler@{PROJECT}.iam.gserviceaccount.com"
MARKER = "Managed by flip-auto/prepare-live.py"
REPOSITORY = f"{REGION}-docker.pkg.dev/{PROJECT}/flip-auto/monitor"
SCHEDULE = "every 30 minutes from 07:00 to 18:00"
SECRETS = {
    "EMAIL_USERNAME": "flip-auto-email-username",
    "EMAIL_APP_PASSWORD": "flip-auto-email-app-password",
    "CLOUD_CMA_WEBHOOK_SECRET": "flip-auto-cma-webhook-secret",
    "ZOHO_EMAIL_USERNAME": "flip-auto-zoho-email-username",
    "ZOHO_EMAIL_APP_PASSWORD": "flip-auto-zoho-email-app-password",
    "CLOUD_CMA_API_KEY": "flip-auto-cloud-cma-api-key",
    "TELEGRAM_BOT_TOKEN": "flip-auto-telegram-bot-token",
    "TELEGRAM_CHAT_ID": "flip-auto-telegram-chat-id",
    "FLIP_AUTO_LIVE_SETTINGS_JSON": "flip-auto-live-settings",
}
NEW_SECRETS = tuple(list(SECRETS.values())[-4:])
DATABASE_CONDITION = {
    "title": "flip-auto-live-db-only",
    "expression": f"resource.name=='projects/{PROJECT}/databases/{DATABASE}'",
}
APIS = {"iam.googleapis.com", "run.googleapis.com", "firestore.googleapis.com",
        "secretmanager.googleapis.com", "cloudscheduler.googleapis.com"}


class PreparationError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise PreparationError(message)


class Gcloud:
    def run(self, *args, missing_ok=False):
        result = subprocess.run(
            ["gcloud", *args, f"--project={PROJECT}", "--quiet", "--format=json",
             "--verbosity=error", "--no-log-http"], capture_output=True, text=True,
            check=False, timeout=600,
            env={**os.environ, "CLOUDSDK_CORE_DISABLE_PROMPTS": "1"},
        )
        if result.returncode:
            if missing_ok and re.search(r"\bNOT_FOUND\b", result.stderr):
                return None
            # Do not echo arbitrary provider error text or operator environment.
            raise PreparationError(f"gcloud {' '.join(args[:3])} failed; exit {result.returncode}")
        try:
            return json.loads(result.stdout) if result.stdout.strip() else {}
        except json.JSONDecodeError as exc:
            raise PreparationError("Unexpected Google JSON response") from exc


def validate_inputs(image, versions, callback_url):
    require(isinstance(image, str) and re.fullmatch(re.escape(REPOSITORY) + r"@sha256:[a-f0-9]{64}", image),
            "Image must be the fixed Artifact Registry repository with an immutable SHA256 digest")
    require(isinstance(callback_url, str) and re.fullmatch(r"https://[A-Za-z0-9.-]+(?::[0-9]+)?/?", callback_url),
            "Callback must be an HTTPS base URL without path, credentials, query, or fragment")
    try:
        parsed = urlsplit(callback_url)
        require(parsed.hostname and parsed.hostname not in (".", "..") and (parsed.port is None or parsed.port > 0),
                "Callback hostname/port is invalid")
    except ValueError as exc:
        raise PreparationError("Callback hostname/port is invalid") from exc
    require(isinstance(versions, dict) and set(versions) == set(SECRETS),
            "Versions JSON must contain exactly the nine runtime environment variable names")
    require(all(isinstance(v, str) and re.fullmatch(r"[1-9][0-9]*", v) for v in versions.values()),
            "Every secret version must be a positive numeric string; latest is forbidden")


def plain_env(callback_url):
    return {"GOOGLE_CLOUD_PROJECT": PROJECT, "FIRESTORE_DATABASE_ID": DATABASE,
            "FLIP_AUTO_EXECUTION_MODE": "live", "CLOUD_CMA_CALLBACK_BASE_URL": callback_url.rstrip("/")}


def verify_job(raw, image, versions, callback_url):
    """Reject replacement/drift, normalizing only documented v1/v2 representations."""
    try:
        v1 = "spec" in raw
        name = raw.get("metadata", {}).get("name") if v1 else raw.get("name")
        require(name in (JOB, f"projects/{PROJECT}/locations/{REGION}/jobs/{JOB}",
                         f"projects/{PROJECT_NUMBER}/locations/{REGION}/jobs/{JOB}"), "Unexpected live job identity")
        execution = raw["spec"]["template"]["spec"] if v1 else raw["template"]
        task = execution["template"]["spec"] if v1 else execution["template"]
        require(execution.get("taskCount") == 1 and execution.get("parallelism") == 1,
                "Live job must have one task and parallelism one")
        account = task.get("serviceAccountName" if v1 else "serviceAccount")
        require(account == RUNTIME_ACCOUNT and task.get("maxRetries") == 0, "Unexpected runtime identity/retry policy")
        # The Cloud Run v1 REST JSON schema represents this int64 field as a
        # decimal string. V2 uses protobuf Duration syntax.
        require(task.get("timeoutSeconds") == "900" if v1 else task.get("timeout") == "900s",
                "Unexpected task timeout")
        allowed_task = {"containers", "maxRetries", "timeoutSeconds", "serviceAccountName"} if v1 else {
            "containers", "maxRetries", "timeout", "serviceAccount", "executionEnvironment"}
        require(not (set(task) - allowed_task), "Unexpected live task settings; no overwrite attempted")
        require(task.get("executionEnvironment", "EXECUTION_ENVIRONMENT_GEN2") == "EXECUTION_ENVIRONMENT_GEN2",
                "Unexpected execution environment")
        containers = task["containers"]
        require(len(containers) == 1, "Live job requires exactly one container")
        container = containers[0]
        require(not (set(container) - {"name", "image", "command", "args", "env", "resources"}),
                "Unexpected live container settings")
        require(container.get("image") == image and container.get("command") == ["python"]
                and container.get("args") == ["gcp_live_runtime.py"], "Live image or entrypoint differs; no overwrite attempted")
        resources = container.get("resources", {})
        require(set(resources) <= {"limits"} and set(resources.get("limits", {})) == {"cpu", "memory"},
                "Unexpected container resources")
        limits = resources["limits"]
        require(limits["cpu"] in ("1", "1000m") and limits["memory"] in ("1Gi", "1024Mi", "1073741824"),
                "Expected one CPU and 1Gi memory")
        actual = {}
        for item in container.get("env", []):
            name = item.get("name")
            require(name and name not in actual, "Duplicate live environment variable")
            if "value" in item:
                require(set(item) == {"name", "value"}, "Ambiguous live environment binding")
                actual[name] = ("value", item["value"])
            else:
                key = "valueFrom" if v1 else "valueSource"
                require(set(item) == {"name", key} and set(item[key]) == {"secretKeyRef"}, "Unexpected secret binding")
                ref = item[key]["secretKeyRef"]
                require(set(ref) == ({"name", "key"} if v1 else {"secret", "version"}), "Unexpected secret reference")
                actual[name] = ("secret", ref["name" if v1 else "secret"], ref["key" if v1 else "version"])
        expected = {key: ("value", value) for key, value in plain_env(callback_url).items()}
        expected.update({key: ("secret", secret, versions[key]) for key, secret in SECRETS.items()})
        require(actual == expected, "Live environment/secret versions differ; no overwrite attempted")
        # Network and encryption options are encoded as annotations by the v1 API.
        if v1:
            for metadata in (raw.get("metadata", {}), raw["spec"]["template"].get("metadata", {}),
                             execution["template"].get("metadata", {})):
                annotations = metadata.get("annotations", {})
                require(not any(any(word in key.lower() for word in ("vpc", "cloudsql", "encryption", "network-interfaces"))
                                for key in annotations), "Unexpected live network/encryption configuration")
    except (KeyError, TypeError, AttributeError) as exc:
        raise PreparationError("Unexpected live job schema") from exc


def verify_scheduler(raw, paused_only=True):
    target = raw.get("httpTarget", {})
    retry = raw.get("retryConfig", {})
    headers = {key.lower(): value for key, value in target.get("headers", {}).items()}
    require(raw.get("name") == f"projects/{PROJECT}/locations/{REGION}/jobs/{JOB}"
            and raw.get("description") == MARKER and raw.get("schedule") == SCHEDULE
            and raw.get("timeZone") == "America/Phoenix"
            and raw.get("state") in (("PAUSED",) if paused_only else ("PAUSED", "ENABLED"))
            and raw.get("attemptDeadline") == "180s"
            and retry.get("retryCount", 0) == 0 and retry.get("maxRetryDuration", "0s") == "0s"
            and headers.get("content-type") == "application/json"
            and target.get("uri") == f"https://run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/jobs/{JOB}:run"
            and target.get("httpMethod") == "POST" and target.get("body") == "e30="
            and target.get("oauthToken") == {"serviceAccountEmail": SCHEDULER_ACCOUNT,
                                              "scope": "https://www.googleapis.com/auth/cloud-platform"}
            and not target.get("oidcToken") and not raw.get("pubsubTarget") and not raw.get("appEngineHttpTarget"),
            "Existing scheduler differs or is active; no overwrite attempted")


def broad_member(member):
    return member in ("allUsers", "allAuthenticatedUsers") or (
        member.startswith("principalSet://cloudresourcemanager.googleapis.com/") and "/type/ServiceAccount" in member)


def member_bindings(policy, member):
    return [binding for binding in policy.get("bindings", []) if member in binding.get("members", [])]


def has_binding(policy, member, role, condition=None):
    return any(b.get("role") == role and b.get("condition") == condition for b in member_bindings(policy, member))


class Preparation:
    def __init__(self, cloud=None):
        self.cloud = cloud or Gcloud()

    def project(self):
        project = self.cloud.run("projects", "describe", PROJECT)
        require(project.get("projectId") == PROJECT and str(project.get("projectNumber")) == PROJECT_NUMBER
                and project.get("lifecycleState") == "ACTIVE", "Expected active flip-auto project 941818435041")
        services = self.cloud.run("services", "list", "--enabled")
        require(APIS <= {s.get("config", {}).get("name") for s in services},
                "Required API is disabled; inspect existing project setup before continuing")
        policy = self.cloud.run("projects", "get-iam-policy", PROJECT)
        for binding in policy.get("bindings", []):
            members = binding.get("members", [])
            require(not any(broad_member(m) for m in members), "Broad project IAM found; inspect inherited invocation/access before continuing")
            require(f"serviceAccount:{SCHEDULER_ACCOUNT}" not in members, "Scheduler account must have no project-level grants")
            if f"serviceAccount:{RUNTIME_ACCOUNT}" in members:
                require(binding.get("role") == "roles/datastore.user" and binding.get("condition") == DATABASE_CONDITION,
                        "Unexpected live runtime project-level grant")
        return policy

    def account(self, account, display_name, apply=False):
        raw = self.cloud.run("iam", "service-accounts", "describe", account, missing_ok=True)
        if raw is None and apply:
            self.cloud.run("iam", "service-accounts", "create", account.split("@", 1)[0],
                           f"--display-name={display_name}", f"--description={MARKER}")
            raw = self.cloud.run("iam", "service-accounts", "describe", account)
        if raw is not None:
            require(raw.get("email") == account and raw.get("projectId") == PROJECT
                    and raw.get("description") == MARKER and raw.get("displayName") == display_name
                    and not raw.get("disabled", False), "Existing service account identity/metadata differs")
        return raw

    def inspect(self):
        policy = self.project()
        account = self.account(RUNTIME_ACCOUNT, "Flip Auto live runtime")
        database = self.cloud.run("firestore", "databases", "describe", f"--database={DATABASE}", missing_ok=True)
        field = None
        if database is not None:
            require(database.get("name") == f"projects/{PROJECT}/databases/{DATABASE}"
                    and database.get("locationId") == REGION and database.get("type") == "FIRESTORE_NATIVE"
                    and database.get("databaseEdition", "STANDARD") == "STANDARD"
                    and database.get("deleteProtectionState") == "DELETE_PROTECTION_ENABLED",
                    "Existing live database configuration differs")
            field = self.cloud.run("firestore", "indexes", "fields", "describe", "state_json",
                                   f"--database={DATABASE}", f"--collection-group={COLLECTION}")
            config = field.get("indexConfig", {})
            require(config.get("usesAncestorConfig", False) or not config.get("indexes", []),
                    "Unexpected custom live state indexes; no overwrite attempted")
        secrets = {}
        for secret in SECRETS.values():
            raw = self.cloud.run("secrets", "describe", secret, missing_ok=secret in NEW_SECRETS)
            if raw is not None:
                require(raw.get("name") == f"projects/{PROJECT_NUMBER}/secrets/{secret}", "Secret identity mismatch")
                if secret in NEW_SECRETS:
                    require(raw.get("replication") == {"automatic": {}}
                            and raw.get("labels", {}).get("provisioner") == "flip-auto-live", "Existing production secret metadata differs")
                secret_policy = self.cloud.run("secrets", "get-iam-policy", secret)
                for binding in member_bindings(secret_policy, f"serviceAccount:{RUNTIME_ACCOUNT}"):
                    require(binding.get("role") == "roles/secretmanager.secretAccessor" and not binding.get("condition"),
                            "Unexpected runtime secret-level grant")
                secrets[secret] = secret_policy
            else:
                secrets[secret] = None
        return {"policy": policy, "account": account, "database": database, "field": field, "secrets": secrets}

    def prepare(self, apply=False):
        before = self.inspect()  # Inspect all preexisting resources before any write.
        if not apply:
            print("Read-only live resource check passed. No values read or changes made.")
            print("Missing resources will be created by --prepare; production remains on GitHub.")
            return before
        self.account(RUNTIME_ACCOUNT, "Flip Auto live runtime", apply=True)
        if before["database"] is None:
            self.cloud.run("firestore", "databases", "create", f"--database={DATABASE}", "--edition=standard",
                           "--type=firestore-native", f"--location={REGION}", "--delete-protection")
        if before["field"] is None or before["field"].get("indexConfig", {}).get("usesAncestorConfig", False):
            self.cloud.run("firestore", "indexes", "fields", "update", "state_json", f"--database={DATABASE}",
                           f"--collection-group={COLLECTION}", "--disable-indexes")
        for secret in NEW_SECRETS:
            if before["secrets"][secret] is None:
                self.cloud.run("secrets", "create", secret, "--replication-policy=automatic", "--labels=provisioner=flip-auto-live")
        member = f"serviceAccount:{RUNTIME_ACCOUNT}"
        if not has_binding(before["policy"], member, "roles/datastore.user", DATABASE_CONDITION):
            self.cloud.run("projects", "add-iam-policy-binding", PROJECT, f"--member={member}", "--role=roles/datastore.user",
                           f"--condition=expression={DATABASE_CONDITION['expression']},title={DATABASE_CONDITION['title']}")
        for secret, policy in before["secrets"].items():
            if policy is None or not has_binding(policy, member, "roles/secretmanager.secretAccessor"):
                self.cloud.run("secrets", "add-iam-policy-binding", secret, f"--member={member}",
                               "--role=roles/secretmanager.secretAccessor", "--condition=None")
        after = self.inspect()
        self.require_prepared(after)
        print("Live database, runtime identity, four production secret containers, and scoped grants verified.")
        print("No secret values read, jobs executed, or schedules created. GitHub and shadow resources unchanged.")
        return after

    def require_prepared(self, state):
        member = f"serviceAccount:{RUNTIME_ACCOUNT}"
        require(state["account"] is not None and state["database"] is not None and state["field"] is not None,
                "Run --prepare before creating the live job")
        config = state["field"].get("indexConfig", {})
        require(not config.get("usesAncestorConfig", False) and not config.get("indexes", []), "Live state indexing is not disabled")
        require(has_binding(state["policy"], member, "roles/datastore.user", DATABASE_CONDITION)
                and all(p is not None and has_binding(p, member, "roles/secretmanager.secretAccessor")
                        for p in state["secrets"].values()), "Live runtime grants are incomplete")

    def create_job(self, image, versions, callback_url, apply=False):
        validate_inputs(image, versions, callback_url)
        self.require_prepared(self.inspect())
        for key, secret in SECRETS.items():
            version = self.cloud.run("secrets", "versions", "describe", versions[key], f"--secret={secret}")
            require(version.get("name") == f"projects/{PROJECT_NUMBER}/secrets/{secret}/versions/{versions[key]}"
                    and version.get("state") == "ENABLED", "Pinned secret version is missing or not ENABLED")
        job_args = ("run", "jobs", "describe", JOB, f"--region={REGION}")
        # Cloud Run emits human prose for a missing job on some CLI versions.
        # Establish absence from a successful list instead of parsing that prose.
        jobs = self.cloud.run("run", "jobs", "list", f"--region={REGION}")
        require(isinstance(jobs, list), "Unexpected Cloud Run job listing")
        matches = [j for j in jobs if (j.get("metadata", {}).get("name") or j.get("name", "")).rsplit("/", 1)[-1] == JOB]
        require(len(matches) <= 1, "Ambiguous live job identity")
        job = self.cloud.run(*job_args) if matches else None
        if job is not None:
            verify_job(job, image, versions, callback_url)
        self.account(SCHEDULER_ACCOUNT, "Flip Auto live invoker")
        scheduler = self.cloud.run("scheduler", "jobs", "describe", JOB, f"--location={REGION}", missing_ok=True)
        policy = self.cloud.run("run", "jobs", "get-iam-policy", JOB, f"--region={REGION}") if job is not None else {}
        granted = self.verify_invoker_policy(policy)
        if scheduler is not None:
            verify_scheduler(scheduler, paused_only=granted)
        require(not granted or scheduler is not None, "Invoker grant exists without the expected paused scheduler")
        if not apply:
            print("Read-only live deployment check passed; pinned versions, job and scheduler metadata verified.")
            return
        if job is None:
            self.cloud.run("run", "jobs", "create", JOB, f"--region={REGION}", f"--image={image}",
                           f"--service-account={RUNTIME_ACCOUNT}", "--tasks=1", "--parallelism=1", "--max-retries=0",
                           "--task-timeout=900s", "--cpu=1", "--memory=1Gi", "--command=python", "--args=gcp_live_runtime.py",
                           "--set-env-vars=" + ",".join(f"{k}={v}" for k, v in plain_env(callback_url).items()),
                           "--set-secrets=" + ",".join(f"{key}={secret}:{versions[key]}" for key, secret in SECRETS.items()),
                           "--labels=app=flip-auto,mode=live")
        verify_job(self.cloud.run(*job_args), image, versions, callback_url)
        # The new scheduler identity has no invocation grant until PAUSED readback.
        self.account(SCHEDULER_ACCOUNT, "Flip Auto live invoker", apply=True)
        self.project()
        current_policy = self.cloud.run("run", "jobs", "get-iam-policy", JOB, f"--region={REGION}")
        require(self.verify_invoker_policy(current_policy) == granted, "Invocation IAM changed during preparation")
        if scheduler is None:
            self.cloud.run("scheduler", "jobs", "create", "http", JOB, f"--location={REGION}", f"--description={MARKER}",
                           f"--schedule={SCHEDULE}", "--time-zone=America/Phoenix",
                           f"--uri=https://run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/jobs/{JOB}:run",
                           "--http-method=POST", "--headers=Content-Type=application/json", "--message-body={}",
                           f"--oauth-service-account-email={SCHEDULER_ACCOUNT}",
                           "--oauth-token-scope=https://www.googleapis.com/auth/cloud-platform",
                           "--attempt-deadline=180s", "--max-retry-attempts=0", "--max-retry-duration=0s")
        if scheduler is None or scheduler.get("state") != "PAUSED":
            self.cloud.run("scheduler", "jobs", "pause", JOB, f"--location={REGION}")
        verify_scheduler(self.cloud.run("scheduler", "jobs", "describe", JOB, f"--location={REGION}"))
        if not granted:
            self.cloud.run("run", "jobs", "add-iam-policy-binding", JOB, f"--region={REGION}",
                           f"--member=serviceAccount:{SCHEDULER_ACCOUNT}", "--role=roles/run.invoker")
        require(self.verify_invoker_policy(self.cloud.run("run", "jobs", "get-iam-policy", JOB, f"--region={REGION}")),
                "Invocation permission readback failed")
        print("Live job and PAUSED scheduler verified. No execution or schedule activation requested.")
        print("Import production state and complete live-cutover gates before the first live execution.")

    @staticmethod
    def verify_invoker_policy(policy):
        member = f"serviceAccount:{SCHEDULER_ACCOUNT}"
        for binding in policy.get("bindings", []):
            require(not any(broad_member(m) for m in binding.get("members", [])), "Broad job invocation IAM is forbidden")
        for binding in member_bindings(policy, member):
            require(binding.get("role") == "roles/run.invoker" and not binding.get("condition"), "Unexpected scheduler job-level IAM")
        return has_binding(policy, member, "roles/run.invoker")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--check", action="store_true", help="Read-only cloud metadata inspection")
    actions.add_argument("--prepare", action="store_true", help="Create isolated live state, identity and secret containers")
    actions.add_argument("--create-job", action="store_true", help="Create exact live job and paused scheduler")
    parser.add_argument("--image")
    parser.add_argument("--versions", type=Path, help="JSON file mapping nine environment names to numeric version strings")
    parser.add_argument("--callback-base-url")
    args = parser.parse_args(argv)
    try:
        operation = Preparation()
        deployment = args.create_job or any((args.image, args.versions, args.callback_base_url))
        if deployment:
            require(not args.prepare and all((args.image, args.versions, args.callback_base_url)),
                    "Deployment requires --image, --versions and --callback-base-url together")
            versions = json.loads(args.versions.read_text(encoding="utf-8"))
            operation.create_job(args.image, versions, args.callback_base_url, apply=args.create_job)
        else:
            operation.prepare(apply=args.prepare)
    except (PreparationError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        # Input JSON parser errors may contain raw input; use a fixed message.
        print(f"Live preparation stopped: {exc if isinstance(exc, PreparationError) else 'local input or command failure'}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
