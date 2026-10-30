"""Minimal, inert state contract for the public CAS canary."""

from __future__ import annotations

import hashlib
import json
from typing import Any

SCHEMA = "p2.synthetic.cas.v1"
EXECUTION_ID = "synthetic-execution-002"
CONTROL_FIELDS = {
    "authority": "NONE",
    "execution_authorized": False,
    "dispatch_status": "NONE",
    "outbox_write": False,
    "continuation_decision": None,
}
SEED_FIELDS = {
    "schema",
    "execution_id",
    "revision",
    "previous_state_sha256",
    "operation_id",
    "event_id",
    "writer_id",
    "proposal",
    *CONTROL_FIELDS,
}


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def state_digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def seed_state() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "execution_id": EXECUTION_ID,
        "revision": 0,
        "previous_state_sha256": None,
        "operation_id": None,
        "event_id": None,
        "writer_id": None,
        "proposal": "synthetic-seed",
        **CONTROL_FIELDS,
    }


def validate_seed(value: dict[str, Any]) -> None:
    if set(value) != SEED_FIELDS:
        raise ValueError("seed fields differ from the closed schema")
    if value["schema"] != SCHEMA or value["execution_id"] != EXECUTION_ID:
        raise ValueError("unexpected seed identity")
    if value["revision"] != 0 or value["previous_state_sha256"] is not None:
        raise ValueError("seed revision is not zero")
    if any(value[key] is not None for key in ("operation_id", "event_id", "writer_id")):
        raise ValueError("seed contains successor identity")
    if value["proposal"] != "synthetic-seed":
        raise ValueError("seed payload mismatch")
    validate_controls(value)


def validate_controls(value: dict[str, Any]) -> None:
    for key, expected in CONTROL_FIELDS.items():
        if value.get(key) is not expected and value.get(key) != expected:
            raise ValueError(f"control invariant changed: {key}")


def successor_state(base: dict[str, Any], run_id: str, writer_id: str) -> dict[str, Any]:
    validate_seed(base)
    if writer_id not in {"writer-a", "writer-b"}:
        raise ValueError("unknown writer")
    if not run_id.isdigit():
        raise ValueError("run id must be numeric")
    return {
        "schema": SCHEMA,
        "execution_id": EXECUTION_ID,
        "revision": 1,
        "previous_state_sha256": state_digest(base),
        "operation_id": f"run-{run_id}-{writer_id}",
        "event_id": f"event-run-{run_id}-{writer_id}",
        "writer_id": writer_id,
        "proposal": f"synthetic-proposal-{writer_id}",
        **CONTROL_FIELDS,
    }


def validate_successor(base: dict[str, Any], candidate: dict[str, Any]) -> None:
    validate_seed(base)
    if set(candidate) != SEED_FIELDS:
        raise ValueError("successor fields differ from the closed schema")
    if candidate["schema"] != SCHEMA or candidate["execution_id"] != EXECUTION_ID:
        raise ValueError("successor identity mismatch")
    if candidate["revision"] != base["revision"] + 1:
        raise ValueError("successor revision is not contiguous")
    if candidate["previous_state_sha256"] != state_digest(base):
        raise ValueError("successor parent digest mismatch")
    writer = candidate["writer_id"]
    if writer not in {"writer-a", "writer-b"}:
        raise ValueError("unknown successor writer")
    if candidate["operation_id"] != f"run-{candidate['operation_id'].split('-')[1]}-{writer}":
        raise ValueError("operation id shape mismatch")
    if candidate["event_id"] != f"event-{candidate['operation_id']}":
        raise ValueError("event id mismatch")
    if candidate["proposal"] != f"synthetic-proposal-{writer}":
        raise ValueError("proposal mismatch")
    validate_controls(candidate)

