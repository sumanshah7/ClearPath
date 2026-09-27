"""Lightweight JSON dummy database + auth for ClearPath PA.

Lives next to the SQLite demo DB under data/. Does not modify pa-engine.
Auth is demo-only: look up by user id, login_name, display_name, or NPI.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DB_DIR = Path(__file__).resolve().parent / "db"
COLLECTIONS = {
    "users",
    "providers",
    "patients",
    "clinics",
    "facilities",
    "payers",
    "coverage_plans",
    "patient_insurance",
    "pa_requests",
    "sessions",
    "audit_log",
}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _path(collection: str) -> Path:
    if collection not in COLLECTIONS:
        raise KeyError(f"Unknown collection: {collection}")
    return DB_DIR / f"{collection}.json"


def _touch_meta() -> None:
    meta_path = DB_DIR / "_meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {"schema_version": 1}
    meta["updated_at"] = _now()
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")


def load(collection: str) -> list[dict[str, Any]]:
    path = _path(collection)
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    if not isinstance(data, list):
        raise TypeError(f"{collection}.json must be a JSON array")
    return data


def save(collection: str, rows: list[dict[str, Any]]) -> None:
    path = _path(collection)
    path.write_text(json.dumps(rows, indent=2) + "\n")
    _touch_meta()


def get_by_id(collection: str, row_id: str) -> dict[str, Any] | None:
    for row in load(collection):
        if row.get("id") == row_id:
            return row
    return None


def upsert(collection: str, row: dict[str, Any]) -> dict[str, Any]:
    rows = load(collection)
    row = dict(row)
    row.setdefault("id", str(uuid.uuid4()))
    row["updated_at"] = _now()
    for i, existing in enumerate(rows):
        if existing.get("id") == row["id"]:
            merged = {**existing, **row}
            rows[i] = merged
            save(collection, rows)
            _audit("upsert", collection, merged["id"], {"before": existing, "after": merged})
            return merged
    row.setdefault("created_at", _now())
    rows.append(row)
    save(collection, rows)
    _audit("insert", collection, row["id"], {"after": row})
    return row


def _norm(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def find_user(id_or_name: str) -> dict[str, Any] | None:
    """Auth lookup: match user id, login_name, display_name, or linked provider NPI."""
    needle = _norm(id_or_name)
    if not needle:
        return None
    for user in load("users"):
        if not user.get("active", True):
            continue
        candidates = [
            user.get("id"),
            user.get("login_name"),
            user.get("display_name"),
            user.get("npi"),
        ]
        if any(_norm(str(c)) == needle for c in candidates if c):
            return user
        # Allow last-name / short name match on display_name
        display = _norm(user.get("display_name"))
        if display and (needle == display.split()[-1] or needle in display):
            return user
    # Clinician can also auth by provider id / full name / NPI from providers.json
    for provider in load("providers"):
        candidates = [provider.get("id"), provider.get("full_name"), provider.get("npi"), provider.get("last_name")]
        if any(_norm(str(c)) == needle for c in candidates if c):
            for user in load("users"):
                if user.get("provider_id") == provider.get("id") and user.get("active", True):
                    return user
            return {
                "id": f"user-from-provider-{provider['id']}",
                "login_name": _norm(provider.get("full_name")),
                "display_name": provider.get("full_name"),
                "role": "clinician",
                "portal": "doctor",
                "provider_id": provider.get("id"),
                "npi": provider.get("npi"),
                "active": True,
                "synthetic": True,
            }
    # Patient portal by patient id / full name / member_id
    for patient in load("patients"):
        candidates = [patient.get("id"), patient.get("full_name"), patient.get("member_id"), patient.get("mrn")]
        if any(_norm(str(c)) == needle for c in candidates if c):
            for user in load("users"):
                if user.get("patient_id") == patient.get("id") and user.get("active", True):
                    return user
            return {
                "id": f"user-from-patient-{patient['id']}",
                "login_name": _norm(patient.get("full_name")),
                "display_name": patient.get("full_name"),
                "role": "patient",
                "portal": "patient",
                "patient_id": patient.get("id"),
                "active": True,
                "synthetic": True,
            }
    return None


def login(id_or_name: str) -> dict[str, Any]:
    """Create a demo session for a user found by id or name. No password."""
    user = find_user(id_or_name)
    if user is None:
        raise LookupError(f"No demo user matches id or name: {id_or_name!r}")
    session = {
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "display_name": user.get("display_name"),
        "role": user.get("role"),
        "portal": user.get("portal"),
        "provider_id": user.get("provider_id"),
        "patient_id": user.get("patient_id"),
        "payer_id": user.get("payer_id"),
        "npi": user.get("npi"),
        "created_at": _now(),
        "expires_at": None,
        "synthetic": True,
    }
    sessions = load("sessions")
    sessions.append(session)
    save("sessions", sessions)
    _audit("login", "sessions", session["id"], {"user_id": user["id"], "via": id_or_name})
    return {"user": user, "session": session}


def logout(session_id: str) -> bool:
    sessions = load("sessions")
    kept = [s for s in sessions if s.get("id") != session_id]
    if len(kept) == len(sessions):
        return False
    save("sessions", kept)
    _audit("logout", "sessions", session_id, {})
    return True


def _audit(action: str, collection: str, target_id: str, detail: dict[str, Any]) -> None:
    rows = load("audit_log")
    rows.append(
        {
            "id": str(uuid.uuid4()),
            "action": action,
            "collection": collection,
            "target_id": target_id,
            "detail": detail,
            "created_at": _now(),
        }
    )
    # Keep the log from growing without bound in local demos
    save("audit_log", rows[-500:])


def snapshot() -> dict[str, int]:
    return {name: len(load(name)) for name in sorted(COLLECTIONS)}


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python data/db_client.py login <id-or-name>")
        print("       python data/db_client.py snapshot")
        raise SystemExit(1)
    cmd = sys.argv[1]
    if cmd == "snapshot":
        print(json.dumps(snapshot(), indent=2))
    elif cmd == "login":
        if len(sys.argv) < 3:
            raise SystemExit("login needs an id or name")
        print(json.dumps(login(" ".join(sys.argv[2:])), indent=2))
    else:
        raise SystemExit(f"Unknown command: {cmd}")
