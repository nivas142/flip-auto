"""Production Cloud Run runner with imported state and durable effect journaling.

This is intentionally separate from the shadow runner. A 20-minute lease must
outlive the deployed 15-minute task timeout, with Cloud Run task retries disabled.
A request whose outcome is uncertain leaves a durable marker and blocks all later
executions until an operator reconciles that outcome.
"""
from __future__ import annotations

import copy
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

UTC = timezone.utc
PROJECT_ID = "flip-auto"
DATABASE_ID = "flip-auto-live"
STATE_COLLECTION = "flip_auto_live_state"
STATE_DOCUMENT = "monitor"
SOURCE_REPOSITORY = "nivas142/flip-auto"
MAX_STATE_BYTES = 700 * 1024
LEASE_SECONDS = 20 * 60
EFFECT_RESERVE_SECONDS = 60
EXECUTION_BUDGET_SECONDS = 14 * 60
# Start before loading monitor or the cloud SDK so their setup consumes budget.
PROCESS_EXECUTION_DEADLINE = time.monotonic() + EXECUTION_BUDGET_SECONDS
SETTINGS_KEYS = {
    "schema_version", "email_lookback_hours", "zoho_imap_host", "zoho_folder",
    "zoho_lookback_hours", "gsheet_public_csv_url", "gsheet_public_url",
    "gsheet_spreadsheet_id",
}
EFFECT_KINDS = {"cma_request", "telegram_alert"}


class RuntimeConfigurationError(ValueError):
    """Safe configuration error; messages contain setting names, never values."""


class LeaseBusy(RuntimeError):
    pass


class LeaseLost(RuntimeError):
    pass


class StateTooLarge(ValueError):
    pass


class UnresolvedEffect(RuntimeError):
    pass


class MonitorFailed(RuntimeError):
    pass


def _value(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "")
    if not isinstance(value, str):
        raise RuntimeConfigurationError(f"Invalid setting: {key}")
    return value.strip()


def _required(env: Mapping[str, str], key: str) -> str:
    value = _value(env, key)
    if not value or "REPLACE_WITH" in value:
        raise RuntimeConfigurationError(f"Missing required setting: {key}")
    return value


def _url(value: str, setting: str, *, sheet: bool = False) -> str:
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "https" and parsed.hostname
            and not parsed.username and not parsed.password
            and (sheet or not parsed.fragment) and "REPLACE_WITH" not in value
        )
        # Force validation of a malformed or out-of-range explicit port.
        parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise RuntimeConfigurationError(f"Invalid HTTPS URL in {setting}")
    return value


def _hours(value: Any, setting: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise RuntimeConfigurationError(f"Invalid hour count in {setting}")
    try:
        result = int(value)
    except ValueError:
        raise RuntimeConfigurationError(f"Invalid hour count in {setting}") from None
    if not 1 <= result <= 168:
        raise RuntimeConfigurationError(f"{setting} must be between 1 and 168 hours")
    return result


def _settings(env: Mapping[str, str]) -> dict[str, Any]:
    try:
        settings = json.loads(_required(env, "FLIP_AUTO_LIVE_SETTINGS_JSON"))
    except (ValueError, TypeError):
        raise RuntimeConfigurationError("Invalid FLIP_AUTO_LIVE_SETTINGS_JSON") from None
    if not isinstance(settings, dict) or set(settings) != SETTINGS_KEYS:
        raise RuntimeConfigurationError("Live settings must contain exactly the reviewed fields")
    if type(settings["schema_version"]) is not int or settings["schema_version"] != 1:
        raise RuntimeConfigurationError("Unsupported live settings schema version")
    for key in SETTINGS_KEYS - {"schema_version", "email_lookback_hours", "zoho_lookback_hours"}:
        if not isinstance(settings[key], str):
            raise RuntimeConfigurationError(f"Invalid live setting: {key}")
        settings[key] = settings[key].strip()
        if "REPLACE_WITH" in settings[key]:
            raise RuntimeConfigurationError(f"Placeholder live setting: {key}")
    for key in ("email_lookback_hours", "zoho_lookback_hours"):
        settings[key] = _hours(settings[key], key)
    if settings["zoho_imap_host"] != "imappro.zoho.com":
        raise RuntimeConfigurationError("Zoho host must match the verified production account")
    if not settings["zoho_folder"] or any(c in settings["zoho_folder"] for c in "\r\n\x00"):
        raise RuntimeConfigurationError("A valid Zoho folder is required")
    csv_url, public_url = settings["gsheet_public_csv_url"], settings["gsheet_public_url"]
    if not (csv_url or public_url):
        raise RuntimeConfigurationError("A production public Google Sheet source is required")
    if csv_url:
        _url(csv_url, "gsheet_public_csv_url", sheet=True)
        settings["gsheet_public_url"] = ""
    else:
        _url(public_url, "gsheet_public_url", sheet=True)
        match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", urlsplit(public_url).path)
        if not match:
            raise RuntimeConfigurationError("Invalid Google Sheet public URL")
        if not settings["gsheet_spreadsheet_id"]:
            settings["gsheet_spreadsheet_id"] = match.group(1)
    if settings["gsheet_spreadsheet_id"] and not re.fullmatch(r"[a-zA-Z0-9_-]+", settings["gsheet_spreadsheet_id"]):
        raise RuntimeConfigurationError("Invalid Google Sheet spreadsheet ID")
    return settings


def build_config(template: dict[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    """Require explicit live mode and transfer the existing production settings."""
    if _value(env, "FLIP_AUTO_EXECUTION_MODE") != "live":
        raise RuntimeConfigurationError("The production runner requires explicit live mode")
    if _required(env, "GOOGLE_CLOUD_PROJECT") != PROJECT_ID:
        raise RuntimeConfigurationError("Unexpected production project")
    if not isinstance(template, dict) or not template:
        raise RuntimeConfigurationError("A nonempty config template is required")
    cfg = copy.deepcopy(template)
    for section in ("email", "valuation", "screening", "gsheet"):
        if not isinstance(cfg.get(section), dict):
            raise RuntimeConfigurationError(f"Missing config section: {section}")
    if not isinstance(cfg["email"].get("cities"), list) or not cfg["email"]["cities"]:
        raise RuntimeConfigurationError("The email city allowlist must not be empty")
    if not isinstance(cfg["email"].get("sender_filters"), list) or not cfg["email"]["sender_filters"]:
        raise RuntimeConfigurationError("The Gmail sender allowlist must not be empty")
    settings = _settings(env)
    if _value(env, "GSHEET_SERVICE_ACCOUNT_JSON"):
        raise RuntimeConfigurationError("The live runner requires reviewed public-sheet access")
    cfg.update({"execution_mode": "live", "strict_errors": True, "read_only_mailbox": True})
    cfg.pop("state_file", None)
    cfg["email"].update({
        "enabled": True,
        "imap_host": "imap.gmail.com",
        "username": _required(env, "EMAIL_USERNAME"),
        "password": _required(env, "EMAIL_APP_PASSWORD"),
        "lookback_hours": settings["email_lookback_hours"],
        "accounts": [{
            "label": "zoho", "enabled": True,
            "imap_host": settings["zoho_imap_host"],
            "username": _required(env, "ZOHO_EMAIL_USERNAME"),
            "password": _required(env, "ZOHO_EMAIL_APP_PASSWORD"),
            "folder": settings["zoho_folder"],
            "lookback_hours": settings["zoho_lookback_hours"],
            "sender_filters": [], "subject_filters": [], "sender_subject_filters": {},
        }],
    })
    cfg["telegram"] = {
        "enabled": True,
        "bot_token": _required(env, "TELEGRAM_BOT_TOKEN"),
        "chat_id": _required(env, "TELEGRAM_CHAT_ID"),
    }
    cfg["twilio"] = {"enabled": False}
    cfg["valuation"].update({
        "enabled": True, "provider": "cloud_cma", "execution_mode": "live",
        "api_key": _required(env, "CLOUD_CMA_API_KEY"),
        "callback_base_url": _url(_required(env, "CLOUD_CMA_CALLBACK_BASE_URL"), "CLOUD_CMA_CALLBACK_BASE_URL"),
        "callback_secret": _required(env, "CLOUD_CMA_WEBHOOK_SECRET"),
        "retain_callback_results": True,
        "template": "Web Leads", "min_listings": 25,
        "max_requests_per_run": 1, "max_requests_per_day": 10,
        "request_ttl_days": 30, "max_report_mb": 200, "max_radius": 1.0,
        "days_old": 180, "size_tolerance": 0.20, "year_tolerance": 10, "minimum_comps": 3,
    })
    cfg["screening"].update({
        "enabled": True, "target_profit": 50000, "selling_cost_percent": 0.07,
        "other_costs": 12000, "max_basis_percent": 0.80,
        "default_rehab_per_sqft": 20, "fallback_rehab": 35000,
    })
    cfg["gsheet"].update({
        "enabled": True,
        "public_csv_url": settings["gsheet_public_csv_url"],
        "public_url": settings["gsheet_public_url"],
        "spreadsheet_id": settings["gsheet_spreadsheet_id"],
        "credentials_json": "",
    })
    return cfg


def serialize_state(state: dict[str, Any]) -> str:
    if not isinstance(state, dict):
        raise RuntimeConfigurationError("Monitor state must be a JSON object")
    try:
        value = json.dumps(state, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError):
        raise RuntimeConfigurationError("Monitor state is not valid JSON") from None
    if len(value.encode("utf-8")) > MAX_STATE_BYTES:
        raise StateTooLarge("Live state exceeds its safe Firestore document budget")
    return value


def validate_import_state(state: dict[str, Any]) -> dict[str, Any]:
    """Validate live tracking state without inventing or seeding production state."""
    encoded = serialize_state(state)
    if not isinstance(state.get("seen"), list) or not all(isinstance(item, str) for item in state["seen"]):
        raise RuntimeConfigurationError("Production state requires its existing seen list")
    last_run = state.get("last_run")
    if not isinstance(last_run, dict) or last_run.get("mode") != "live":
        raise RuntimeConfigurationError("Production state must originate from a live monitor run")
    if state.get("execution_mode", "live") != "live":
        raise RuntimeConfigurationError("Shadow tracking state cannot become production state")
    for key in ("cma_requests", "cma_reports_processed", "cma_reports_rejected", "cma_reports_unavailable"):
        if key in state and not isinstance(state[key], dict):
            raise RuntimeConfigurationError("Malformed production CMA tracking state")
        for request_key, record in state.get(key, {}).items():
            if not isinstance(request_key, str) or not re.fullmatch(r"[0-9a-f]{64}", request_key):
                raise RuntimeConfigurationError("Malformed production CMA tracking key")
            timestamp = record
            if key == "cma_reports_unavailable":
                if not isinstance(record, dict):
                    raise RuntimeConfigurationError("Malformed unavailable CMA tracking record")
                timestamp = record.get("timestamp")
                if "reason" in record and not isinstance(record["reason"], str):
                    raise RuntimeConfigurationError("Malformed unavailable CMA reason")
                if "parser_version" in record and (
                    type(record["parser_version"]) is not int or record["parser_version"] < 1
                ):
                    raise RuntimeConfigurationError("Malformed unavailable CMA parser version")
                if "callback_request_key" in record and (
                    not isinstance(record["callback_request_key"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", record["callback_request_key"])
                ):
                    raise RuntimeConfigurationError("Malformed unavailable CMA callback key")
            try:
                parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00")) if isinstance(timestamp, str) else None
            except ValueError:
                parsed = None
            if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
                raise RuntimeConfigurationError("Malformed production CMA tracking timestamp")
    return json.loads(encoded)


class FirestoreLiveStore:
    """Atomic lease, imported-state gate, and write-ahead side-effect journal."""

    def __init__(self, document: Any, transact: Callable[[Callable[[Any], Any]], Any], *,
                 owner: str | None = None, clock: Callable[[], datetime] | None = None,
                 monotonic_clock: Callable[[], float] | None = None,
                 execution_deadline: float | None = None) -> None:
        self.document = document
        self.transact = transact
        self.owner = owner or uuid.uuid4().hex
        self.clock = clock or (lambda: datetime.now(UTC))
        self.monotonic_clock = monotonic_clock or time.monotonic
        self.execution_deadline = (
            execution_deadline if execution_deadline is not None
            else self.monotonic_clock() + EXECUTION_BUDGET_SECONDS
        )
        self._inflight: dict[str, Any] | None = None

    @staticmethod
    def _data(snapshot: Any) -> dict[str, Any]:
        if not snapshot.exists:
            raise RuntimeConfigurationError("Production state has not been imported")
        data = snapshot.to_dict()
        if not isinstance(data, dict) or data.get("execution_mode") != "live" or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
            raise RuntimeConfigurationError("Refusing non-live or malformed stored state")
        if type(data.get("enabled")) is not bool:
            raise RuntimeConfigurationError("Production state is missing its explicit activation gate")
        if not {"inflight_effect", "lease_owner", "lease_until"} <= data.keys():
            raise RuntimeConfigurationError("Production state is missing its execution controls")
        if data["lease_owner"] is None and data["lease_until"] is not None:
            raise RuntimeConfigurationError("Malformed production lease")
        if (
            data.get("source_repository") != SOURCE_REPOSITORY
            or not isinstance(data.get("source_commit"), str)
            or not re.fullmatch(r"[0-9a-f]{40}", data["source_commit"])
            or not isinstance(data.get("source_state_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", data["source_state_sha256"])
            or not isinstance(data.get("imported_at"), datetime)
            or data["imported_at"].tzinfo is None
        ):
            raise RuntimeConfigurationError("Production state is missing its GitHub import provenance")
        return data

    @staticmethod
    def _state(data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(data.get("state_json"), str):
            raise RuntimeConfigurationError("Stored live state is missing its JSON payload")
        try:
            state = json.loads(data["state_json"])
        except (ValueError, TypeError):
            raise RuntimeConfigurationError("Stored live state contains invalid JSON") from None
        return validate_import_state(state)

    def _lease(self, data: dict[str, Any], *, reserve: int = 0) -> datetime:
        now = self.clock()
        until = data.get("lease_until")
        if (
            data.get("lease_owner") != self.owner or not isinstance(until, datetime)
            or until.tzinfo is None or until <= now + timedelta(seconds=reserve)
        ):
            raise LeaseLost("Production lease lost or insufficient time remains; no new effect is allowed")
        return now

    def acquire(self) -> dict[str, Any]:
        def acquire_in_transaction(transaction: Any) -> dict[str, Any]:
            data = self._data(self.document.get(transaction=transaction))
            if not data["enabled"]:
                raise RuntimeConfigurationError("Production is disabled pending cutover verification")
            if data.get("inflight_effect") is not None:
                raise UnresolvedEffect("An uncertain production effect requires operator reconciliation")
            now = self.clock()
            until = data.get("lease_until")
            if data.get("lease_owner"):
                if not isinstance(until, datetime) or until.tzinfo is None:
                    raise RuntimeConfigurationError("Malformed production lease")
                if until > now:
                    raise LeaseBusy("Another production execution owns the lease")
            state = self._state(data)
            data.update({"lease_owner": self.owner, "lease_until": now + timedelta(seconds=LEASE_SECONDS)})
            transaction.set(self.document, data)
            return state
        state = self.transact(acquire_in_transaction)
        self._inflight = None
        return state

    def begin(self, kind: str, key: str, state: dict[str, Any]) -> None:
        if kind not in EFFECT_KINDS or not isinstance(key, str) or not key or len(key) > 256:
            raise RuntimeConfigurationError("Invalid production effect identifier")
        state_json = serialize_state(state)
        def check_execution_budget() -> None:
            if self.monotonic_clock() + EFFECT_RESERVE_SECONDS >= self.execution_deadline:
                raise LeaseLost("Insufficient task execution time remains; no new effect is allowed")
        def begin_in_transaction(transaction: Any) -> dict[str, Any]:
            data = self._data(self.document.get(transaction=transaction))
            now = self._lease(data, reserve=EFFECT_RESERVE_SECONDS)
            check_execution_budget()
            if not data["enabled"]:
                raise RuntimeConfigurationError("Production was disabled before the next effect")
            if data.get("inflight_effect") is not None:
                raise UnresolvedEffect("An uncertain production effect requires operator reconciliation")
            marker = {"kind": kind, "key": key, "owner": self.owner, "started_at": now}
            data.update({"state_json": state_json, "inflight_effect": marker, "updated_at": now})
            transaction.set(self.document, data)
            return marker
        self._inflight = self.transact(begin_in_transaction)
        # A delayed commit response must not allow a late external request.
        # Its committed journal remains for reconciliation if this fails.
        check_execution_budget()

    def complete(self, state: dict[str, Any]) -> None:
        state_json = serialize_state(state)
        def complete_in_transaction(transaction: Any) -> None:
            data = self._data(self.document.get(transaction=transaction))
            now = self._lease(data)
            marker = data.get("inflight_effect")
            if not self._inflight or marker != self._inflight:
                raise UnresolvedEffect("Production effect journal ownership does not match")
            data.update({"state_json": state_json, "inflight_effect": None, "updated_at": now})
            transaction.set(self.document, data)
        self.transact(complete_in_transaction)
        self._inflight = None

    def checkpoint(self, state: dict[str, Any]) -> None:
        state_json = serialize_state(state)
        def checkpoint_in_transaction(transaction: Any) -> None:
            data = self._data(self.document.get(transaction=transaction))
            now = self._lease(data)
            if data.get("inflight_effect") is not None:
                raise UnresolvedEffect("Cannot checkpoint across an uncertain production effect")
            data.update({"state_json": state_json, "updated_at": now})
            transaction.set(self.document, data)
        self.transact(checkpoint_in_transaction)

    def has_inflight(self) -> bool:
        def inspect(transaction: Any) -> bool:
            data = self._data(self.document.get(transaction=transaction))
            self._lease(data)
            return data.get("inflight_effect") is not None
        return self.transact(inspect)

    def _release(self, state_json: str | None, outcome: str) -> None:
        def release_in_transaction(transaction: Any) -> None:
            data = self._data(self.document.get(transaction=transaction))
            now = self._lease(data)
            if state_json is not None:
                data["state_json"] = state_json
            # Never clear an uncertain effect simply because a task exited.
            data.update({"lease_owner": None, "lease_until": None, "updated_at": now,
                         "last_outcome": "failed" if data.get("inflight_effect") is not None else outcome})
            transaction.set(self.document, data)
        self.transact(release_in_transaction)

    def finish(self, state: dict[str, Any], *, outcome: str) -> None:
        if outcome not in {"success", "failed"}:
            raise RuntimeConfigurationError("Invalid production outcome")
        self._release(serialize_state(state), outcome)

    def abandon(self) -> None:
        self._release(None, "failed_state_not_saved")


def make_store(env: Mapping[str, str], *, execution_deadline: float | None = None) -> FirestoreLiveStore:
    if _required(env, "GOOGLE_CLOUD_PROJECT") != PROJECT_ID:
        raise RuntimeConfigurationError("Unexpected production project")
    if _required(env, "FIRESTORE_DATABASE_ID") != DATABASE_ID:
        raise RuntimeConfigurationError("The live runner requires its dedicated production database")
    from google.cloud import firestore
    client = firestore.Client(project=PROJECT_ID, database=DATABASE_ID)
    document = client.collection(STATE_COLLECTION).document(STATE_DOCUMENT)
    def transact(callback: Callable[[Any], Any]) -> Any:
        return firestore.transactional(callback)(client.transaction())
    return FirestoreLiveStore(document, transact, execution_deadline=execution_deadline)


def run_live(config: dict[str, Any], store: FirestoreLiveStore,
             runner: Callable[[dict[str, Any], dict[str, Any]], int]) -> int:
    if config.get("execution_mode") != "live" or not config.get("strict_errors"):
        raise RuntimeConfigurationError("The production runner requires strict live configuration")
    if not isinstance(config.get("valuation"), dict):
        raise RuntimeConfigurationError("Missing production valuation configuration")
    state = store.acquire()
    config["_effect_guard"] = store
    config["valuation"]["_effect_guard"] = store
    outcome = "failed"
    try:
        result = runner(config, state)
        if store.has_inflight():
            raise UnresolvedEffect("An uncertain production effect requires operator reconciliation")
        if result != 0 or state.get("last_run", {}).get("errors", 0):
            raise MonitorFailed("Monitor reported an unsuccessful production execution")
        outcome = "success"
    finally:
        if config.get("_execution_name"):
            state.setdefault("last_run", {})["execution_name"] = config["_execution_name"]
        try:
            store.finish(state, outcome=outcome)
        except (StateTooLarge, RuntimeConfigurationError):
            store.abandon()
            raise
    return 0


def main() -> int:
    try:
        execution_name = _required(os.environ, "CLOUD_RUN_EXECUTION")
        if not re.fullmatch(r"flip-auto-live-[a-z0-9]+(?:-[a-z0-9]+)*", execution_name) or len(execution_name) > 63:
            raise RuntimeConfigurationError("Unexpected Cloud Run production execution name")
        import yaml
        from monitor import run_monitor
        template = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
        config = build_config(template, os.environ)
        config["_execution_name"] = execution_name
        return run_live(config, make_store(os.environ, execution_deadline=PROCESS_EXECUTION_DEADLINE), run_monitor)
    except Exception as exc:
        safe = str(exc) if isinstance(exc, (
            RuntimeConfigurationError, LeaseBusy, LeaseLost, StateTooLarge, UnresolvedEffect, MonitorFailed,
        )) else type(exc).__name__
        print(f"[ERROR] GCP production execution failed ({safe})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
