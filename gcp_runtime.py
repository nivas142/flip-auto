"""Phase-one Cloud Run runner: isolated, read-only shadow execution only.

Deploy with a 15-minute Cloud Run task timeout and zero task retries. The
20-minute Firestore lease therefore outlives a timed-out task; overlapping
executions fail closed. This module deliberately has no live-mode switch.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit


UTC = timezone.utc
STATE_COLLECTION = "flip_auto_shadow_state"
STATE_DOCUMENT = "monitor"
MAX_STATE_BYTES = 700 * 1024
LEASE_SECONDS = 20 * 60


class RuntimeConfigurationError(ValueError):
    """Invalid configuration; messages must never contain secret values."""


class LeaseBusy(RuntimeError):
    pass


class LeaseLost(RuntimeError):
    pass


class StateTooLarge(ValueError):
    pass


def _value(env: Mapping[str, str], key: str, default: str = "") -> str:
    return str(env.get(key, default)).strip()


def _required(env: Mapping[str, str], key: str) -> str:
    value = _value(env, key)
    if not value or "REPLACE_WITH" in value:
        raise RuntimeConfigurationError(f"Missing required setting: {key}")
    return value


def _https_url(value: str, setting: str) -> str:
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "https" and parsed.hostname
            and not parsed.username and not parsed.password and not parsed.fragment
            and "REPLACE_WITH" not in value
        )
    except ValueError:
        valid = False
    if not valid:
        raise RuntimeConfigurationError(f"Invalid HTTPS URL in {setting}")
    return value


def _lookback(env: Mapping[str, str], setting: str, default: int) -> int:
    try:
        value = int(_value(env, setting) or default)
    except (TypeError, ValueError):
        raise RuntimeConfigurationError(f"Invalid hour count in {setting}") from None
    if not 1 <= value <= 168:
        raise RuntimeConfigurationError(f"{setting} must be between 1 and 168 hours")
    return value


def build_config(template: dict[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    """Build an in-memory config without persisting or printing credentials."""
    if _value(env, "FLIP_AUTO_EXECUTION_MODE", "shadow") != "shadow":
        raise RuntimeConfigurationError("The GCP runner only supports shadow mode")
    if not isinstance(template, dict) or not template:
        raise RuntimeConfigurationError("A nonempty config template is required")
    cfg = copy.deepcopy(template)
    for section in ("email", "valuation", "screening", "gsheet"):
        if not isinstance(cfg.get(section), dict):
            raise RuntimeConfigurationError(f"Missing config section: {section}")
    cfg["execution_mode"] = "shadow"
    # A live runner will require a separate reviewed cutover. These redundant
    # guards keep accidental requests/notifications disabled in this phase.
    cfg["telegram"] = {"enabled": False}
    cfg["twilio"] = {"enabled": False}
    cfg.pop("state_file", None)
    email_cfg = cfg["email"]
    if not isinstance(email_cfg.get("cities"), list) or not email_cfg["cities"]:
        raise RuntimeConfigurationError("The email city allowlist must not be empty")
    email_cfg.update({
        "enabled": True,
        "imap_host": "imap.gmail.com",
        "username": _required(env, "EMAIL_USERNAME"),
        "password": _required(env, "EMAIL_APP_PASSWORD"),
        "lookback_hours": _lookback(env, "EMAIL_LOOKBACK_HOURS", 48),
        "accounts": [],
    })
    zoho_username = _value(env, "ZOHO_EMAIL_USERNAME")
    zoho_password = _value(env, "ZOHO_EMAIL_APP_PASSWORD")
    if bool(zoho_username) != bool(zoho_password):
        raise RuntimeConfigurationError("Both Zoho credential settings are required together")
    if zoho_username:
        email_cfg["accounts"].append({
            "label": "zoho",
            "enabled": True,
            "imap_host": _value(env, "ZOHO_IMAP_HOST") or "imap.zoho.com",
            "username": _required(env, "ZOHO_EMAIL_USERNAME"),
            "password": _required(env, "ZOHO_EMAIL_APP_PASSWORD"),
            "folder": _value(env, "ZOHO_FOLDER") or "Off-Market-Deals",
            "lookback_hours": _lookback(env, "ZOHO_LOOKBACK_HOURS", 48),
            "sender_filters": [],
            "subject_filters": [],
            "sender_subject_filters": {},
        })
    cfg["valuation"].update({
        "enabled": True,
        "provider": "cloud_cma",
        "execution_mode": "shadow",
        "api_key": "",
        "callback_base_url": _https_url(
            _required(env, "CLOUD_CMA_CALLBACK_BASE_URL"), "CLOUD_CMA_CALLBACK_BASE_URL"
        ),
        "callback_secret": _required(env, "CLOUD_CMA_WEBHOOK_SECRET"),
        "max_requests_per_run": 0,
        "max_requests_per_day": 0,
    })
    cfg["screening"]["enabled"] = True
    # Public-sheet reads are optional. Phase one never writes a service-account
    # JSON key to disk; supporting authenticated Sheets can be added via ADC.
    sheet_cfg = cfg["gsheet"]
    csv_url = _value(env, "GSHEET_PUBLIC_CSV_URL")
    sheet_url = _value(env, "GSHEET_PUBLIC_URL")
    if _value(env, "GSHEET_SERVICE_ACCOUNT_JSON"):
        raise RuntimeConfigurationError("Phase-one shadow only supports public-sheet access")
    sheet_cfg.update({
        "enabled": bool(csv_url or sheet_url),
        "public_csv_url": _https_url(csv_url, "GSHEET_PUBLIC_CSV_URL") if csv_url else "",
        "public_url": _https_url(sheet_url, "GSHEET_PUBLIC_URL") if sheet_url and not csv_url else "",
        "credentials_json": "",
        "spreadsheet_id": "",
    })
    return cfg


def serialize_state(state: dict[str, Any]) -> str:
    if not isinstance(state, dict):
        raise RuntimeConfigurationError("Monitor state must be a JSON object")
    value = json.dumps(state, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(value.encode("utf-8")) > MAX_STATE_BYTES:
        raise StateTooLarge("Shadow state exceeds its safe Firestore document budget")
    return value


def load_seed(path: str) -> dict[str, Any]:
    if not path:
        return {}
    source = Path(path)
    if source.stat().st_size > MAX_STATE_BYTES:
        raise StateTooLarge("Seed state exceeds the shadow state budget")
    state = json.loads(source.read_text(encoding="utf-8"))
    serialize_state(state)
    return state


class FirestoreShadowStore:
    """A transactionally leased document in a dedicated shadow collection.

    Only ``state_json`` contains monitor state; deploy its single-field index
    exemption before use. It is capped well below Firestore's 1 MiB limit.
    The transaction callback is injected so unit tests need no GCP packages.
    """

    def __init__(
        self,
        document: Any,
        transact: Callable[[Callable[[Any], Any]], Any],
        *,
        owner: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.document = document
        self.transact = transact
        self.owner = owner or uuid.uuid4().hex
        self.clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def _data(snapshot: Any) -> dict[str, Any]:
        if not snapshot.exists:
            return {}
        data = snapshot.to_dict()
        if not isinstance(data, dict) or data.get("execution_mode") != "shadow":
            raise RuntimeConfigurationError("Refusing non-shadow or malformed stored state")
        return data

    @staticmethod
    def _state(data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(data.get("state_json"), str):
            raise RuntimeConfigurationError("Stored shadow state is missing its JSON payload")
        state = json.loads(data["state_json"])
        serialize_state(state)
        return state

    def acquire(self, seed: dict[str, Any] | None = None) -> dict[str, Any]:
        seed_json = serialize_state(seed if seed is not None else {})

        def acquire_in_transaction(transaction: Any) -> dict[str, Any]:
            data = self._data(self.document.get(transaction=transaction))
            now = self.clock()
            lease_until = data.get("lease_until")
            if data.get("lease_owner"):
                if not isinstance(lease_until, datetime) or lease_until.tzinfo is None:
                    raise RuntimeConfigurationError("Malformed shadow lease")
                if lease_until > now:
                    raise LeaseBusy("Another shadow execution owns the lease")
            if data:
                state = self._state(data)
            else:
                state = json.loads(seed_json)
                data = {"execution_mode": "shadow", "state_json": seed_json, "schema_version": 1}
            data.update({"lease_owner": self.owner, "lease_until": now + timedelta(seconds=LEASE_SECONDS)})
            transaction.set(self.document, data)
            return state

        return self.transact(acquire_in_transaction)

    def _release(self, state_json: str | None, outcome: str) -> None:
        def release_in_transaction(transaction: Any) -> None:
            data = self._data(self.document.get(transaction=transaction))
            now = self.clock()
            lease_until = data.get("lease_until")
            if (
                data.get("lease_owner") != self.owner
                or not isinstance(lease_until, datetime)
                or lease_until.tzinfo is None
                or lease_until <= now
            ):
                raise LeaseLost("Shadow lease ownership was lost; state was not saved")
            if state_json is not None:
                data["state_json"] = state_json
            data.update({"lease_owner": None, "lease_until": None, "updated_at": now, "last_outcome": outcome})
            transaction.set(self.document, data)

        self.transact(release_in_transaction)

    def finish(self, state: dict[str, Any], *, outcome: str) -> None:
        self._release(serialize_state(state), outcome)

    def abandon(self) -> None:
        """Release only our unexpired lease, retaining the previous state."""
        self._release(None, "failed_state_not_saved")


def make_store(env: Mapping[str, str]) -> FirestoreShadowStore:
    project = _required(env, "GOOGLE_CLOUD_PROJECT")
    database = _value(env, "FIRESTORE_DATABASE_ID", "flip-auto")
    if not database or database == "(default)" or "/" in database:
        raise RuntimeConfigurationError("Use a named dedicated Firestore database")
    # ADC resolves the Cloud Run service identity. No exported GCP key is used.
    from google.cloud import firestore

    client = firestore.Client(project=project, database=database)
    document = client.collection(STATE_COLLECTION).document(STATE_DOCUMENT)

    def transact(callback: Callable[[Any], Any]) -> Any:
        return firestore.transactional(callback)(client.transaction())

    return FirestoreShadowStore(document, transact)


def run_shadow(
    config: dict[str, Any],
    store: FirestoreShadowStore,
    runner: Callable[[dict[str, Any], dict[str, Any]], int],
    *,
    seed: dict[str, Any] | None = None,
) -> int:
    if config.get("execution_mode") != "shadow":
        raise RuntimeConfigurationError("The GCP runner only supports shadow mode")
    state = store.acquire(seed)
    outcome = "failed"
    try:
        result = runner(config, state)
        if result != 0:
            raise RuntimeError("Monitor reported an unsuccessful shadow execution")
        outcome = "success"
    finally:
        try:
            store.finish(state, outcome=outcome)
        except (StateTooLarge, TypeError, ValueError):
            # Invalid/oversized state must not overwrite the last good snapshot.
            # abandon validates ownership too, so a newer holder is untouched.
            store.abandon()
            raise
    return 0


def main() -> int:
    try:
        import yaml
        from monitor import run_monitor

        template = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
        config = build_config(template, os.environ)
        seed = load_seed(_value(os.environ, "FLIP_AUTO_INITIAL_STATE_PATH"))
        store = make_store(os.environ)
        return run_shadow(config, store, run_monitor, seed=seed)
    except Exception as exc:
        # SDK/network exception strings may contain credentials or callback URLs.
        # Only our own explicitly safe errors may include a diagnostic message.
        safe_detail = str(exc) if isinstance(
            exc, (RuntimeConfigurationError, LeaseBusy, LeaseLost, StateTooLarge)
        ) else type(exc).__name__
        print(f"[ERROR] GCP shadow execution failed ({safe_detail})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
