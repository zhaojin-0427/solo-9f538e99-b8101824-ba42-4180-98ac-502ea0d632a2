"""Pure assembly helpers shared by the service and the trace ledger.

Nothing in this module touches the database connection or the HTTP layer.
Keeping the hashing and assembly math here lets the upgrade migration rebuild
the exact same snapshots that the live service records.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from .config import SAMPLES_PER_MINUTE


# --------------------------------------------------------------------------- #
# Canonical time / JSON helpers
# --------------------------------------------------------------------------- #
def minute_key(dt: datetime) -> str:
    """Canonical UTC minute string, e.g. ``2026-11-17T05:06:00Z``."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:00Z")


def parse_minute(raw: str) -> datetime:
    return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def dump_samples(samples: list[int]) -> str:
    return json.dumps(samples, separators=(",", ":"))


def load_samples(raw: str) -> list[int]:
    return json.loads(raw)


def content_hash(start_sample: int, samples: list[int]) -> str:
    payload = json.dumps(
        {"start_sample": start_sample, "samples": samples}, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Fragment snapshots
# --------------------------------------------------------------------------- #
def fragment_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    """Convert a sqlite row (or mapping) into the public fragment shape."""
    return {
        "station": row["station"],
        "minute_utc": parse_minute(row["minute_utc"]),
        "fragment_no": row["fragment_no"],
        "revision": row["revision"],
        "start_sample": row["start_sample"],
        "samples": load_samples(row["samples"]),
        "content_hash": row["content_hash"],
    }


def fragment_snapshot(
    rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Canonical, JSON-serialisable effective fragment set.

    Only the fields needed to rebuild the assembly are kept, sorted by
    ``fragment_no`` so the snapshot hash is stable.
    """
    snapshot = [
        {
            "fragment_no": r["fragment_no"],
            "revision": r["revision"],
            "start_sample": r["start_sample"],
            "samples": load_samples(r["samples"]),
            "content_hash": r["content_hash"],
        }
        for r in rows
    ]
    snapshot.sort(key=lambda f: f["fragment_no"])
    return snapshot


def snapshot_hash(snapshot: list[dict[str, Any]]) -> str:
    """Stable hash of a fragment snapshot (same value as older releases)."""
    canonical = [
        {
            "fragment_no": f["fragment_no"],
            "revision": f["revision"],
            "start_sample": f["start_sample"],
            "samples": f["samples"],
            "content_hash": f["content_hash"],
        }
        for f in sorted(snapshot, key=lambda f: f["fragment_no"])
    ]
    blob = json.dumps(canonical, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# --------------------------------------------------------------------------- #
# Assembly math
# --------------------------------------------------------------------------- #
def compute_assembly_from_snapshot(
    snapshot: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Coverage, gaps and value conflicts for a fragment snapshot."""
    # position -> list of (fragment_no, value)
    covered: dict[int, list[tuple[int, int]]] = {
        p: [] for p in range(SAMPLES_PER_MINUTE)
    }
    for frag in snapshot:
        start = frag["start_sample"]
        for offset, value in enumerate(frag["samples"]):
            covered[start + offset].append((frag["fragment_no"], value))

    gaps: list[int] = []
    conflicts: list[dict[str, Any]] = []
    coverage = 0
    for pos in range(SAMPLES_PER_MINUTE):
        entries = covered[pos]
        if not entries:
            gaps.append(pos)
            continue
        coverage += 1
        distinct_values = {value for _, value in entries}
        if len(distinct_values) > 1:
            conflicts.append(
                {
                    "position": pos,
                    "fragment_nos": [frag_no for frag_no, _ in entries],
                    "values": [value for _, value in entries],
                }
            )
    return {
        "complete": coverage == SAMPLES_PER_MINUTE and not conflicts,
        "coverage": coverage,
        "gaps": gaps,
        "conflicts": conflicts,
    }


def compute_assembly(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Assembly for raw sqlite rows (kept for callers that hold rows)."""
    return compute_assembly_from_snapshot(fragment_snapshot(rows))
