"""Business logic: fragment uploads, minute assembly and publications.

All mutating operations run inside a single ``BEGIN IMMEDIATE`` SQLite
transaction so that uploads and publications serialize on one consistent
snapshot of the fragment set.  Any error rolls the whole transaction back —
no half-written publications or trace events are ever left behind.

Every accepted fragment change also appends an immutable ``trace_events`` row
carrying its global sequence, the full effective fragment set *after* the
change and, when it replaces an earlier revision, that earlier revision.
Publications record the sequence that created them, and each invalidated
publication records the change that expired it.  Because all of this is
committed together, history and invalidation attribution always describe the
same committed state.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from .assembly import (
    compute_assembly,
    compute_assembly_from_snapshot,
    content_hash,
    dump_samples,
    fragment_dict,
    fragment_snapshot,
    load_samples,
    minute_key,
    parse_minute,
    snapshot_hash,
)
from .config import SAMPLES_PER_MINUTE
from .database import get_conn, read_snapshot


# --------------------------------------------------------------------------- #
# Errors (mapped to HTTP status codes in the router)
# --------------------------------------------------------------------------- #
class ServiceError(Exception):
    status_code = 400


class BoundaryError(ServiceError):
    status_code = 422


class StaleRevisionError(ServiceError):
    """Incoming revision is not higher than the one already stored."""

    status_code = 409


class ContentConflictError(ServiceError):
    """Same fragment_no + revision but different payload."""

    status_code = 409


class NotPublishableError(ServiceError):
    status_code = 409


class TraceQueryError(ServiceError):
    status_code = 422


# --------------------------------------------------------------------------- #
# Trace helpers
# --------------------------------------------------------------------------- #
def _json_snapshot(snapshot: list[dict[str, Any]]) -> str:
    return json.dumps(snapshot, separators=(",", ":"))


def _snapshot_from_event(event: sqlite3.Row) -> list[dict[str, Any]]:
    return json.loads(event["snapshot_json"])


def _record_event(
    conn: sqlite3.Connection,
    *,
    station: str,
    minute: str,
    kind: str,
    snapshot: list[dict[str, Any]],
    prior_snapshot_hash: str | None = None,
    fragment_no: int | None = None,
    revision: int | None = None,
    previous: dict[str, Any] | None = None,
    current: dict[str, Any] | None = None,
    publication_id: int | None = None,
) -> tuple[int, str]:
    """Append one trace event and return ``(seq, snapshot_hash)``."""
    new_hash = snapshot_hash(snapshot)
    previous = previous or {}
    current = current or {}
    cur = conn.execute(
        "INSERT INTO trace_events (station, minute_utc, kind, fragment_no, "
        "revision, previous_revision, previous_start_sample, previous_samples, "
        "previous_content_hash, start_sample, samples, content_hash, "
        "publication_id, snapshot_hash, prior_snapshot_hash, snapshot_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            station,
            minute,
            kind,
            fragment_no,
            revision,
            previous.get("revision"),
            previous.get("start_sample"),
            dump_samples(previous["samples"]) if previous else None,
            previous.get("content_hash"),
            current.get("start_sample", previous.get("start_sample")),
            dump_samples(current["samples"]) if current else None,
            current.get("content_hash"),
            publication_id,
            new_hash,
            prior_snapshot_hash,
            _json_snapshot(snapshot),
        ),
    )
    return int(cur.lastrowid), new_hash


def _effective_snapshot(
    conn: sqlite3.Connection, station: str, minute: str
) -> list[dict[str, Any]]:
    return fragment_snapshot(_get_fragments(conn, station, minute))


def _invalidate_publications(
    conn: sqlite3.Connection, station: str, minute: str, event_seq: int
) -> list[int]:
    """Mark every currently-fresh publication stale and pin the cause.

    Returns the publication ids that were expired by ``event_seq``.
    """
    rows = conn.execute(
        "SELECT id FROM publications WHERE station = ? AND minute_utc = ? "
        "AND stale = 0 ORDER BY id",
        (station, minute),
    ).fetchall()
    ids = [row["id"] for row in rows]
    if not ids:
        return []
    conn.execute(
        "UPDATE publications SET stale = 1, invalidated_event_seq = ? "
        "WHERE station = ? AND minute_utc = ? AND stale = 0",
        (event_seq, station, minute),
    )
    conn.executemany(
        "INSERT OR REPLACE INTO publication_invalidations "
        "(publication_id, event_seq) VALUES (?, ?)",
        [(pub_id, event_seq) for pub_id in ids],
    )
    return ids


# --------------------------------------------------------------------------- #
# Upload
# --------------------------------------------------------------------------- #
def upload_fragment(payload: dict[str, Any]) -> dict[str, Any]:
    station = payload["station"]
    minute = minute_key(payload["minute_utc"])
    fragment_no = payload["fragment_no"]
    revision = payload["revision"]
    start_sample = payload["start_sample"]
    samples = payload["samples"]

    # A fragment must stay inside the 60 one-hertz sample slots.
    if start_sample + len(samples) > SAMPLES_PER_MINUTE:
        raise BoundaryError(
            f"fragment exceeds minute boundary: start_sample={start_sample} "
            f"length={len(samples)} would reach slot {start_sample + len(samples)} "
            f"(limit {SAMPLES_PER_MINUTE})"
        )

    new_hash = content_hash(start_sample, samples)
    samples_json = dump_samples(samples)
    current = {
        "start_sample": start_sample,
        "samples": samples,
        "content_hash": new_hash,
    }

    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT * FROM fragments WHERE station = ? AND minute_utc = ? "
                "AND fragment_no = ?",
                (station, minute, fragment_no),
            ).fetchone()

            if existing is not None:
                if revision < existing["revision"]:
                    raise StaleRevisionError(
                        f"fragment_no={fragment_no} already at revision "
                        f"{existing['revision']}; incoming revision {revision} "
                        "is not higher"
                    )
                if revision == existing["revision"]:
                    # Same version: identical content is an idempotent retry and
                    # must return the original result; different content is refused.
                    if existing["content_hash"] == new_hash:
                        result = fragment_dict(existing)
                        result["status"] = "unchanged"
                        conn.commit()
                        return result
                    raise ContentConflictError(
                        f"fragment_no={fragment_no} revision={revision} already "
                        "exists with different content"
                    )

                # Higher revision accepted: the old revision belongs to history
                # and can never be rewritten, even by a later correction.
                prior_snapshot = _effective_snapshot(conn, station, minute)
                prior_hash = snapshot_hash(prior_snapshot)
                previous = {
                    "revision": existing["revision"],
                    "start_sample": existing["start_sample"],
                    "samples": load_samples(existing["samples"]),
                    "content_hash": existing["content_hash"],
                }
                conn.execute(
                    "UPDATE fragments SET revision = ?, start_sample = ?, "
                    "samples = ?, content_hash = ? WHERE id = ?",
                    (revision, start_sample, samples_json, new_hash, existing["id"]),
                )
                kind = "fragment_revised"
            else:
                prior_snapshot = _effective_snapshot(conn, station, minute)
                prior_hash = snapshot_hash(prior_snapshot)
                previous = None
                conn.execute(
                    "INSERT INTO fragments (station, minute_utc, fragment_no, "
                    "revision, start_sample, samples, content_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        station,
                        minute,
                        fragment_no,
                        revision,
                        start_sample,
                        samples_json,
                        new_hash,
                    ),
                )
                kind = "fragment_added"

            # Snapshot after the change, committed in the same transaction.
            post_snapshot = _effective_snapshot(conn, station, minute)
            event_seq, _ = _record_event(
                conn,
                station=station,
                minute=minute,
                kind=kind,
                snapshot=post_snapshot,
                prior_snapshot_hash=prior_hash,
                fragment_no=fragment_no,
                revision=revision,
                previous=previous,
                current=current,
            )

            # The effective fragment set changed: every earlier publication of
            # this minute is now superseded and attributed to this exact change.
            _invalidate_publications(conn, station, minute, event_seq)

            row = conn.execute(
                "SELECT * FROM fragments WHERE station = ? AND minute_utc = ? "
                "AND fragment_no = ?",
                (station, minute, fragment_no),
            ).fetchone()
            conn.commit()
            result = fragment_dict(row)
            result["status"] = "accepted" if existing is None else "superseded"
            return result
        except Exception:
            conn.rollback()
            raise


# --------------------------------------------------------------------------- #
# Reads (current state — unchanged public API)
# --------------------------------------------------------------------------- #
def get_assembly(station: str, minute_dt: datetime) -> dict[str, Any]:
    minute = minute_key(minute_dt)
    with get_conn() as conn:
        rows = _get_fragments(conn, station, minute)
        fragments = [fragment_dict(r) for r in rows]
        assembly = compute_assembly(rows)
    return {
        "station": station,
        "minute_utc": parse_minute(minute),
        **assembly,
        "fragments": fragments,
    }


def list_fragments(station: str, minute_dt: datetime) -> list[dict[str, Any]]:
    minute = minute_key(minute_dt)
    with get_conn() as conn:
        rows = _get_fragments(conn, station, minute)
        return [fragment_dict(r) for r in rows]


def _get_fragments(
    conn: sqlite3.Connection, station: str, minute: str
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM fragments WHERE station = ? AND minute_utc = ? "
        "ORDER BY fragment_no",
        (station, minute),
    ).fetchall()


def _refresh_staleness(conn: sqlite3.Connection, pub: sqlite3.Row) -> sqlite3.Row:
    """Self-heal the stale flag from the current fragment set on read.

    This only happens for records written before the trace ledger existed whose
    legacy ``stale`` flag was already wrong.  The invalidation is attributed to
    the latest recorded fragment change of that minute so the trace view and
    the publication view agree on the cause; if no change was recorded (the
    set cannot actually differ then) the flag alone is corrected.
    """
    rows = _get_fragments(conn, pub["station"], pub["minute_utc"])
    current_hash = snapshot_hash(fragment_snapshot(rows))
    if not pub["stale"] and current_hash != pub["fragment_set_hash"]:
        cause = conn.execute(
            "SELECT MAX(seq) AS seq FROM trace_events "
            "WHERE station = ? AND minute_utc = ? "
            "AND kind IN ('fragment_added', 'fragment_revised')",
            (pub["station"], pub["minute_utc"]),
        ).fetchone()["seq"]
        conn.execute(
            "UPDATE publications SET stale = 1, invalidated_event_seq = ? "
            "WHERE id = ?",
            (cause, pub["id"]),
        )
        if cause is not None:
            conn.execute(
                "INSERT OR IGNORE INTO publication_invalidations "
                "(publication_id, event_seq) VALUES (?, ?)",
                (pub["id"], cause),
            )
        pub = conn.execute(
            "SELECT * FROM publications WHERE id = ?", (pub["id"],)
        ).fetchone()
    return pub


def _hydrate_publication(conn: sqlite3.Connection, pub: sqlite3.Row) -> dict[str, Any]:
    items = conn.execute(
        "SELECT * FROM publication_items WHERE publication_id = ? ORDER BY fragment_no",
        (pub["id"],),
    ).fetchall()
    return {
        "id": pub["id"],
        "station": pub["station"],
        "minute_utc": parse_minute(pub["minute_utc"]),
        "wave": load_samples(pub["wave"]),
        "stale": bool(pub["stale"]),
        "fragment_set_hash": pub["fragment_set_hash"],
        "created_at": pub["created_at"],
        "items": [
            {
                "fragment_no": it["fragment_no"],
                "revision": it["revision"],
                "start_sample": it["start_sample"],
                "samples": load_samples(it["samples"]),
                "content_hash": it["content_hash"],
            }
            for it in items
        ],
    }


def list_publications(
    station: str | None, minute_dt: datetime | None
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if station is not None:
        clauses.append("station = ?")
        params.append(station)
    if minute_dt is not None:
        clauses.append("minute_utc = ?")
        params.append(minute_key(minute_dt))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_conn() as conn:
        pubs = conn.execute(
            f"SELECT * FROM publications{where} ORDER BY id", params
        ).fetchall()
        out = []
        for pub in pubs:
            pub = _refresh_staleness(conn, pub)
            out.append(_hydrate_publication(conn, pub))
        conn.commit()
        return out


def get_publication(pub_id: int) -> dict[str, Any] | None:
    with get_conn() as conn:
        pub = conn.execute(
            "SELECT * FROM publications WHERE id = ?", (pub_id,)
        ).fetchone()
        if pub is None:
            return None
        pub = _refresh_staleness(conn, pub)
        result = _hydrate_publication(conn, pub)
        conn.commit()
        return result


def get_latest_publication(station: str, minute_dt: datetime) -> dict[str, Any] | None:
    minute = minute_key(minute_dt)
    with get_conn() as conn:
        pub = conn.execute(
            "SELECT * FROM publications WHERE station = ? AND minute_utc = ? "
            "ORDER BY id DESC LIMIT 1",
            (station, minute),
        ).fetchone()
        if pub is None:
            return None
        pub = _refresh_staleness(conn, pub)
        result = _hydrate_publication(conn, pub)
        conn.commit()
        return result


# --------------------------------------------------------------------------- #
# Publish
# --------------------------------------------------------------------------- #
def publish_minute(station: str, minute_dt: datetime) -> dict[str, Any]:
    minute = minute_key(minute_dt)
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = _get_fragments(conn, station, minute)
            assembly = compute_assembly(rows)

            if not rows:
                raise NotPublishableError("no fragments for this station-minute")
            if assembly["gaps"]:
                raise NotPublishableError(
                    f"minute incomplete, gaps at positions: "
                    f"{','.join(map(str, assembly['gaps']))}"
                )
            if assembly["conflicts"]:
                detail = "; ".join(
                    f"position {c['position']} fragments {c['fragment_nos']} "
                    f"values {c['values']}"
                    for c in assembly["conflicts"]
                )
                raise NotPublishableError(f"unresolved conflicts: {detail}")

            snapshot = fragment_snapshot(rows)
            set_hash = snapshot_hash(snapshot)

            # A fresh publication for this exact fragment set already exists:
            # return it instead of creating a duplicate.
            existing = conn.execute(
                "SELECT * FROM publications WHERE station = ? AND minute_utc = ? "
                "AND fragment_set_hash = ? AND stale = 0 ORDER BY id DESC LIMIT 1",
                (station, minute, set_hash),
            ).fetchone()
            if existing is not None:
                result = _hydrate_publication(conn, existing)
                conn.commit()
                return result

            # Build the 60-sample wave from the committed fragment set.
            wave = [0] * SAMPLES_PER_MINUTE
            for r in rows:
                start = r["start_sample"]
                for offset, value in enumerate(load_samples(r["samples"])):
                    wave[start + offset] = value

            cur = conn.execute(
                "INSERT INTO publications (station, minute_utc, wave, "
                "fragment_set_hash, stale) VALUES (?, ?, ?, ?, 0)",
                (station, minute, dump_samples(wave), set_hash),
            )
            pub_id = int(cur.lastrowid)
            conn.executemany(
                "INSERT INTO publication_items (publication_id, fragment_no, "
                "revision, start_sample, samples, content_hash) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        pub_id,
                        r["fragment_no"],
                        r["revision"],
                        r["start_sample"],
                        r["samples"],
                        r["content_hash"],
                    )
                    for r in rows
                ],
            )
            event_seq, _ = _record_event(
                conn,
                station=station,
                minute=minute,
                kind="publish",
                snapshot=snapshot,
                publication_id=pub_id,
            )
            conn.execute(
                "UPDATE publications SET created_event_seq = ? WHERE id = ?",
                (event_seq, pub_id),
            )
            pub = conn.execute(
                "SELECT * FROM publications WHERE id = ?", (pub_id,)
            ).fetchone()
            result = _hydrate_publication(conn, pub)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise


# --------------------------------------------------------------------------- #
# Traceability
# --------------------------------------------------------------------------- #
_FRAGMENT_EVENT_KINDS = ("fragment_added", "fragment_revised")


def _load_events(
    conn: sqlite3.Connection, station: str, minute: str
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM trace_events WHERE station = ? AND minute_utc = ? "
        "ORDER BY seq",
        (station, minute),
    ).fetchall()


def _publications_at(
    conn: sqlite3.Connection,
    station: str,
    minute: str,
    resolved_seq: int,
    resolved_hash: str | None,
) -> list[dict[str, Any]]:
    """Publications that existed and described the effective set at ``seq``.

    Expired publications are deliberately *not* listed here; the selected
    change's effect on them is reported separately in ``expired_publications``.
    """
    if resolved_hash is None:
        return []
    pubs = conn.execute(
        "SELECT * FROM publications WHERE station = ? AND minute_utc = ? "
        "ORDER BY id",
        (station, minute),
    ).fetchall()
    out = []
    for pub in pubs:
        created_seq = pub["created_event_seq"]
        # created_seq NULL = written by an older release, i.e. before the
        # trace baseline, therefore before every resolvable sequence.
        existed = created_seq is None or created_seq <= resolved_seq
        if not existed or pub["fragment_set_hash"] != resolved_hash:
            continue
        out.append(
            {
                "id": pub["id"],
                "fragment_set_hash": pub["fragment_set_hash"],
                "created_event_seq": created_seq,
                "created_at": pub["created_at"] if created_seq is not None else None,
                "creation_time_known": created_seq is not None,
                "fresh": True,
                "invalidated_event_seq": None,
                "invalidated_at": None,
            }
        )
    return out


def _event_view(
    event: sqlite3.Row, minute_seqs: list[int]
) -> dict[str, Any]:
    recorded = event["samples"]
    previous = event["previous_samples"]
    return {
        "seq": event["seq"],
        "minute_seq": minute_seqs.index(event["seq"]) + 1,
        "kind": event["kind"],
        "recorded_at": event["recorded_at"],
        "fragment_no": event["fragment_no"],
        "revision": event["revision"],
        "previous_revision": event["previous_revision"],
        "start_sample": event["start_sample"],
        "samples": load_samples(recorded) if recorded is not None else None,
        "content_hash": event["content_hash"],
        "previous_start_sample": event["previous_start_sample"],
        "previous_samples": load_samples(previous) if previous is not None else None,
        "previous_content_hash": event["previous_content_hash"],
        "snapshot_hash": event["snapshot_hash"],
        "fragment_added": event["kind"] == "fragment_added",
        "unknown_before": bool(event["unknown_before"]),
        "publication_id": event["publication_id"],
    }


def _expired_by(
    conn: sqlite3.Connection, event_seq: int
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT p.id, p.fragment_set_hash, p.created_event_seq, p.created_at, "
        "p.creation_time_known, i.event_seq, i.recorded_at AS invalidated_at "
        "FROM publication_invalidations i "
        "JOIN publications p ON p.id = i.publication_id "
        "WHERE i.event_seq = ? ORDER BY p.id",
        (event_seq,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "fragment_set_hash": r["fragment_set_hash"],
            "created_event_seq": r["created_event_seq"],
            "created_at": r["created_at"] if r["created_event_seq"] is not None else None,
            "creation_time_known": bool(r["creation_time_known"]),
            "invalidated_by_seq": r["event_seq"],
            "invalidated_at": r["invalidated_at"],
        }
        for r in rows
    ]


def trace_minute(
    station: str,
    minute_dt: datetime,
    seq: int | None = None,
    fragment_no: int | None = None,
) -> dict[str, Any]:
    """Reconstruct a station-minute exactly as committed at a sequence.

    ``seq`` selects the greatest committed event at or before it; the state is
    that event's immutable snapshot, so later corrections never rewrite it.
    Without ``seq`` the latest accepted change is used (optionally restricted
    to ``fragment_no``).
    """
    minute = minute_key(minute_dt)
    if seq is not None and seq < 0:
        raise TraceQueryError("seq must be >= 0")
    if seq is not None and fragment_no is not None:
        raise TraceQueryError("use either seq or fragment_no, not both")

    with get_conn() as conn, read_snapshot(conn):
        events = _load_events(conn, station, minute)
        if not events:
            raise LookupError("no trace history for this station-minute")

        seqs = [e["seq"] for e in events]
        baseline = next((e for e in events if e["kind"] == "baseline"), None)
        # Without a baseline this database was born with the ledger, so history
        # is complete from sequence 0.  With a baseline, nothing before it was
        # saved.
        history_known_from = baseline["seq"] if baseline is not None else 0
        fragment_events = [e for e in events if e["kind"] in _FRAGMENT_EVENT_KINDS]
        latest_snapshot = _snapshot_from_event(events[-1])
        latest_nos = {f["fragment_no"] for f in latest_snapshot}

        state_known = True
        unknown_reason: str | None = None
        chosen: sqlite3.Row | None = None
        state_after_seq: int | None = None  # state = empty after this seq

        if seq is not None:
            if baseline is not None and seq < baseline["seq"]:
                # This minute already had data when the trace baseline was
                # captured, but the changes that produced it were never saved.
                state_known = False
                unknown_reason = (
                    f"no saved history before seq {baseline['seq']} for this "
                    "station-minute; earlier changes are unknown"
                )
                resolved_seq = baseline["seq"]
            elif seq < seqs[0]:
                # The minute itself starts later than this sequence, so it was
                # definitely empty then (no event, hence no fragment, yet).
                resolved_seq = seq
                state_after_seq = seq
            else:
                chosen = max(
                    (e for e in events if e["seq"] <= seq), key=lambda e: e["seq"]
                )
                resolved_seq = chosen["seq"]
        else:
            if fragment_no is not None:
                matching = [e for e in fragment_events if e["fragment_no"] == fragment_no]
                if matching:
                    chosen = matching[-1]
                elif fragment_no in latest_nos:
                    # Present at upgrade / baseline but its arrival was never
                    # recorded: show the current revision, flag the unknown past.
                    chosen = events[-1]
                else:
                    raise LookupError(
                        f"no accepted change for fragment_no={fragment_no}"
                    )
            else:
                chosen = events[-1]
            resolved_seq = chosen["seq"]

        snapshot = _snapshot_from_event(chosen) if chosen is not None else []
        resolved_hash = chosen["snapshot_hash"] if chosen is not None else None

        # Mark revisions that a *later* change superseded without touching the
        # state reconstructed for the requested sequence.  A baseline event's
        # snapshot is the state *at upgrade time*, so only changes recorded
        # strictly after it count as corrections.
        later_changes: dict[int, int] = {}
        for e in fragment_events:
            if e["seq"] > resolved_seq and e["fragment_no"] is not None:
                later_changes.setdefault(e["fragment_no"], e["seq"])
        fragments = []
        for f in snapshot:
            later_seq = later_changes.get(f["fragment_no"])
            fragments.append({**f, "superseded_later": later_seq is not None,
                              "superseded_at_seq": later_seq})

        # Numbering holes are derived *only* from this sequence's snapshot, so a
        # fragment that arrives later cannot retroactively change an old result.
        present = {f["fragment_no"] for f in snapshot}
        missing = (
            [n for n in range(0, max(present) + 1) if n not in present]
            if present
            else []
        )

        assembly = (
            compute_assembly_from_snapshot(snapshot)
            if state_known and (chosen is not None or state_after_seq is not None)
            else None
        )
        if assembly is not None:
            assembly = {**assembly, "missing_fragment_nos": missing}

        publications = _publications_at(
            conn, station, minute, resolved_seq, resolved_hash
        )

        expired = (
            _expired_by(conn, chosen["seq"])
            if chosen is not None and chosen["kind"] in _FRAGMENT_EVENT_KINDS
            else []
        )

        next_seq = next((s for s in seqs if s > resolved_seq), None)
        return {
            "station": station,
            "minute_utc": parse_minute(minute),
            "requested_seq": seq,
            "resolved_seq": resolved_seq,
            "state_known": state_known,
            "unknown_reason": unknown_reason,
            "history_known_from_seq": history_known_from,
            "unknown_before_baseline": baseline is not None,
            "event": _event_view(chosen, seqs) if chosen is not None else None,
            "fragments": fragments if state_known else [],
            "assembly": assembly if state_known else None,
            "publications": publications if state_known else [],
            "expired_publications": expired if state_known else [],
            "next_seq": next_seq,
            "events_total": len(events),
        }


def list_trace_events(
    station: str, minute_dt: datetime
) -> dict[str, Any]:
    """Full ordered change log of a station-minute."""
    minute = minute_key(minute_dt)
    with get_conn() as conn, read_snapshot(conn):
        events = _load_events(conn, station, minute)
        if not events:
            raise LookupError("no trace history for this station-minute")
        seqs = [e["seq"] for e in events]
        return {
            "station": station,
            "minute_utc": parse_minute(minute),
            "events": [_event_view(e, seqs) for e in events],
            "next_seq": None,
        }
