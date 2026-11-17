"""End-to-end tests for the waveform assembly API.

Each test gets an isolated SQLite file so runs never touch real data.
"""
from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    tmpdir = tempfile.mkdtemp(prefix="wf-test-")
    db_path = Path(tmpdir) / "test.db"
    monkeypatch.setenv("WAVEFORM_DB_PATH", str(db_path))

    # Import after the env var is set so config picks up the test database.
    import importlib

    from app import config, database, main, service

    # Reload in dependency order so every module re-reads the test DB path.
    importlib.reload(config)
    importlib.reload(database)
    importlib.reload(service)
    importlib.reload(main)
    database.init_db()
    with TestClient(main.app) as c:
        yield c
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()
    os.rmdir(tmpdir)


MIN = "2026-11-17T05:06:00Z"


def frag(no, rev, start, samples, station="STA1", minute=MIN):
    return {
        "station": station,
        "minute_utc": minute,
        "fragment_no": no,
        "revision": rev,
        "start_sample": start,
        "samples": samples,
    }


# --------------------------------------------------------------------------- #
# Health & validation
# --------------------------------------------------------------------------- #
def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_boundary_rejected(client):
    # start 55 + 6 samples reaches slot 61 -> out of bounds
    r = client.post("/fragments", json=frag(1, 1, 55, [1, 2, 3, 4, 5, 6]))
    assert r.status_code == 422
    assert "boundary" in r.json()["detail"]


def test_non_whole_minute_rejected(client):
    payload = frag(1, 1, 0, [1])
    payload["minute_utc"] = "2026-11-17T05:06:30Z"
    r = client.post("/fragments", json=payload)
    assert r.status_code == 422


def test_revision_must_be_positive(client):
    r = client.post("/fragments", json=frag(1, 0, 0, [1]))
    assert r.status_code == 422


def test_empty_samples_rejected(client):
    r = client.post("/fragments", json=frag(1, 1, 0, []))
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Revision semantics
# --------------------------------------------------------------------------- #
def test_upload_and_retrieve(client):
    r = client.post("/fragments", json=frag(1, 1, 0, [10, 20]))
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "accepted"
    assert body["revision"] == 1
    assert len(body["content_hash"]) == 64

    r = client.get("/fragments", params={"station": "STA1", "minute_utc": MIN})
    assert r.status_code == 200
    assert len(r.json()) == 1


def test_lower_revision_rejected(client):
    client.post("/fragments", json=frag(1, 3, 0, [10, 20]))
    r = client.post("/fragments", json=frag(1, 2, 0, [99]))
    assert r.status_code == 409
    assert "not higher" in r.json()["detail"]
    # stored fragment untouched
    r = client.get("/fragments", params={"station": "STA1", "minute_utc": MIN})
    assert r.json()[0]["revision"] == 3


def test_same_revision_same_content_idempotent(client):
    r1 = client.post("/fragments", json=frag(1, 2, 0, [10, 20]))
    r2 = client.post("/fragments", json=frag(1, 2, 0, [10, 20]))
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r2.json()["status"] == "unchanged"
    assert r1.json()["content_hash"] == r2.json()["content_hash"]
    r = client.get("/fragments", params={"station": "STA1", "minute_utc": MIN})
    assert len(r.json()) == 1


def test_same_revision_different_content_rejected(client):
    client.post("/fragments", json=frag(1, 2, 0, [10, 20]))
    r = client.post("/fragments", json=frag(1, 2, 0, [10, 21]))
    assert r.status_code == 409
    assert "different content" in r.json()["detail"]
    r = client.get("/fragments", params={"station": "STA1", "minute_utc": MIN})
    assert r.json()[0]["samples"] == [10, 20]


def test_higher_revision_supersedes(client):
    client.post("/fragments", json=frag(1, 1, 0, [10, 20]))
    r = client.post("/fragments", json=frag(1, 2, 0, [11, 21]))
    assert r.status_code == 201
    assert r.json()["status"] == "superseded"
    r = client.get("/fragments", params={"station": "STA1", "minute_utc": MIN})
    assert r.json()[0]["revision"] == 2
    assert r.json()[0]["samples"] == [11, 21]


# --------------------------------------------------------------------------- #
# Assembly: gaps and conflicts
# --------------------------------------------------------------------------- #
def test_assembly_gaps(client):
    client.post("/fragments", json=frag(1, 1, 0, [1, 2, 3]))
    r = client.get("/assembly", params={"station": "STA1", "minute_utc": MIN})
    body = r.json()
    assert body["complete"] is False
    assert body["coverage"] == 3
    assert body["gaps"] == list(range(3, 60))
    assert body["conflicts"] == []


def test_assembly_conflict_reports_position_and_fragments(client):
    client.post("/fragments", json=frag(1, 1, 0, [1, 2, 3]))
    client.post("/fragments", json=frag(2, 1, 2, [9, 4]))  # overlap at slot 2
    r = client.get("/assembly", params={"station": "STA1", "minute_utc": MIN})
    body = r.json()
    assert body["complete"] is False
    assert body["conflicts"] == [
        {"position": 2, "fragment_nos": [1, 2], "values": [3, 9]}
    ]
    assert body["gaps"] == list(range(4, 60))


def test_overlap_same_value_is_not_conflict(client):
    client.post("/fragments", json=frag(1, 1, 0, [1, 2, 3]))
    client.post("/fragments", json=frag(2, 1, 2, [3, 4]))  # slot 2 agrees
    r = client.get("/assembly", params={"station": "STA1", "minute_utc": MIN})
    body = r.json()
    assert body["conflicts"] == []
    assert body["coverage"] == 4
    assert body["gaps"] == list(range(4, 60))


def test_complete_minute_assembly(client):
    client.post("/fragments", json=frag(1, 1, 0, list(range(0, 30))))
    client.post("/fragments", json=frag(2, 1, 30, list(range(30, 60))))
    r = client.get("/assembly", params={"station": "STA1", "minute_utc": MIN})
    body = r.json()
    assert body["complete"] is True
    assert body["coverage"] == 60
    assert body["gaps"] == []
    assert body["conflicts"] == []


# --------------------------------------------------------------------------- #
# Publishing
# --------------------------------------------------------------------------- #
def test_publish_fails_with_gaps(client):
    client.post("/fragments", json=frag(1, 1, 0, [1, 2]))
    r = client.post("/publications", params={"station": "STA1", "minute_utc": MIN})
    assert r.status_code == 409
    assert "gaps" in r.json()["detail"]


def test_publish_fails_with_conflict(client):
    client.post("/fragments", json=frag(1, 1, 0, [1] + [0] * 29))
    client.post("/fragments", json=frag(2, 1, 0, [9] + [0] * 59))
    r = client.post("/publications", params={"station": "STA1", "minute_utc": MIN})
    assert r.status_code == 409
    assert "conflicts" in r.json()["detail"]
    # failed publish leaves nothing behind
    r = client.get("/publications", params={"station": "STA1"})
    assert r.json() == []


def test_full_publish_lifecycle(client):
    wave_a = list(range(60))
    client.post("/fragments", json=frag(1, 1, 0, wave_a[:30]))
    client.post("/fragments", json=frag(2, 1, 30, wave_a[30:]))

    r = client.post("/publications", params={"station": "STA1", "minute_utc": MIN})
    assert r.status_code == 201
    pub = r.json()
    pub_id = pub["id"]
    assert pub["wave"] == wave_a
    assert pub["stale"] is False
    assert sorted(it["fragment_no"] for it in pub["items"]) == [1, 2]
    assert {it["revision"] for it in pub["items"]} == {1}
    # revisions and per-fragment waveforms are pinned in the record
    pinned = {it["fragment_no"]: it["samples"] for it in pub["items"]}
    assert pinned[1] == wave_a[:30]
    assert pinned[2] == wave_a[30:]

    # publishing the same fragment set again is idempotent
    r2 = client.post("/publications", params={"station": "STA1", "minute_utc": MIN})
    assert r2.json()["id"] == pub_id

    # a later correction changes current assembly but must not mutate the record
    client.post("/fragments", json=frag(1, 2, 0, [100] + wave_a[1:30]))
    r = client.get(f"/publications/{pub_id}")
    old = r.json()
    assert old["wave"] == wave_a  # frozen waveform
    assert old["stale"] is True
    assert old["items"][0]["revision"] == 1  # frozen revisions

    # the new fragment set can itself be published
    new_wave = [100] + wave_a[1:]
    r = client.post("/publications", params={"station": "STA1", "minute_utc": MIN})
    assert r.status_code == 201
    pub2 = r.json()
    assert pub2["id"] != pub_id
    assert pub2["wave"] == new_wave
    assert pub2["stale"] is False

    # both records remain readable; latest endpoint returns the new one
    r = client.get(
        "/publications/latest",
        params={"station": "STA1", "minute_utc": MIN},
    )
    assert r.json()["id"] == pub2["id"]
    r = client.get("/publications", params={"station": "STA1", "minute_utc": MIN})
    ids = [p["id"] for p in r.json()]
    assert ids == [pub_id, pub2["id"]]
    assert [p["stale"] for p in r.json()] == [True, False]


def test_supersede_marks_previous_publication_stale(client):
    client.post("/fragments", json=frag(1, 1, 0, list(range(0, 30))))
    client.post("/fragments", json=frag(2, 1, 30, list(range(30, 60))))
    pub = client.post(
        "/publications", params={"station": "STA1", "minute_utc": MIN}
    ).json()
    assert pub["stale"] is False

    # replacement fragment at same number, higher revision, different span
    client.post("/fragments", json=frag(2, 2, 29, [77] + list(range(30, 60))))
    r = client.get(f"/publications/{pub['id']}")
    assert r.json()["stale"] is True


def test_idempotent_retry_does_not_stale_publication(client):
    client.post("/fragments", json=frag(1, 1, 0, list(range(0, 30))))
    client.post("/fragments", json=frag(2, 1, 30, list(range(30, 60))))
    pub = client.post(
        "/publications", params={"station": "STA1", "minute_utc": MIN}
    ).json()

    # same version, same content retransmission: fragment set unchanged
    client.post("/fragments", json=frag(2, 1, 30, list(range(30, 60))))
    r = client.get(f"/publications/{pub['id']}")
    assert r.json()["stale"] is False
    # no duplicate publication either
    again = client.post(
        "/publications", params={"station": "STA1", "minute_utc": MIN}
    ).json()
    assert again["id"] == pub["id"]


# --------------------------------------------------------------------------- #
# Restart durability
# --------------------------------------------------------------------------- #
def test_data_survives_restart(client, monkeypatch, tmp_path):
    client.post("/fragments", json=frag(1, 1, 0, list(range(0, 30))))
    client.post("/fragments", json=frag(2, 1, 30, list(range(30, 60))))
    pub = client.post(
        "/publications", params={"station": "STA1", "minute_utc": MIN}
    ).json()

    # Simulate a process restart: rebuild app objects against the same file.
    import importlib

    from app import config, database, main, service

    importlib.reload(config)
    importlib.reload(database)
    importlib.reload(service)
    importlib.reload(main)
    with TestClient(main.app) as restarted:
        r = restarted.get(f"/publications/{pub['id']}")
        assert r.status_code == 200
        body = r.json()
        assert body["wave"] == list(range(60))
        assert body["stale"] is False
        r = restarted.get(
            "/assembly", params={"station": "STA1", "minute_utc": MIN}
        )
        assert r.json()["complete"] is True


# --------------------------------------------------------------------------- #
# Concurrency: interleaved uploads and publishes serialize on one snapshot
# --------------------------------------------------------------------------- #
def test_concurrent_uploads_and_publishes(monkeypatch):
    """Hit a real uvicorn server with parallel uploads/publishes.

    Every publication must come from one committed fragment set and failed
    publishes must never leave partial rows behind.
    """
    import socket
    import time

    import httpx
    import uvicorn

    tmpdir = tempfile.mkdtemp(prefix="wf-live-")
    db_path = Path(tmpdir) / "live.db"
    monkeypatch.setenv("WAVEFORM_DB_PATH", str(db_path))

    import importlib

    from app import config, database, main, service

    importlib.reload(config)
    importlib.reload(database)
    importlib.reload(service)
    importlib.reload(main)
    database.init_db()

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    server = uvicorn.Server(
        uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if httpx.get(base + "/health", timeout=1).status_code == 200:
                break
        except httpx.TransportError:
            time.sleep(0.1)
    else:  # pragma: no cover
        raise RuntimeError("test server did not start")

    errors: list[Exception] = []
    barrier = threading.Barrier(6)

    def worker(idx: int) -> None:
        try:
            no = idx + 1
            barrier.wait()
            r = httpx.post(
                base + "/fragments",
                json=frag(no, 1, (no - 1) * 10, [no * 100] * 10),
                timeout=10,
            )
            assert r.status_code == 201, r.text
            rp = httpx.post(
                base + "/publications",
                params={"station": "STA1", "minute_utc": MIN},
                timeout=10,
            )
            # incomplete snapshots fail cleanly; the complete one succeeds —
            # a 500/half-written row would be a correctness bug
            assert rp.status_code in (201, 409), rp.text
        except Exception as exc:  # pragma: no cover - failure reporting
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors

    with httpx.Client(base_url=base, timeout=10) as http:
        r = http.get("/fragments", params={"station": "STA1", "minute_utc": MIN})
        assert len(r.json()) == 6
        pubs = http.post(
            "/publications", params={"station": "STA1", "minute_utc": MIN}
        ).json()
        assert pubs["wave"] == [
            v for no in range(1, 7) for v in [no * 100] * 10
        ]
        listing = http.get(
            "/publications", params={"station": "STA1", "minute_utc": MIN}
        ).json()
        # only one publication row for a single fragment set
        assert len(listing) == 1

    server.should_exit = True
    thread.join(timeout=10)


def test_station_isolation(client):
    client.post("/fragments", json=frag(1, 1, 0, [1, 2, 3], station="STA1"))
    client.post("/fragments", json=frag(1, 1, 0, [4, 5, 6], station="STA2"))
    r = client.get("/fragments", params={"station": "STA2", "minute_utc": MIN})
    assert r.json()[0]["samples"] == [4, 5, 6]
    r = client.get("/assembly", params={"station": "STA1", "minute_utc": MIN})
    assert r.json()["coverage"] == 3


# --------------------------------------------------------------------------- #
# Traceability (拼接质量追溯)
# --------------------------------------------------------------------------- #
def trace(client, seq=None, fragment_no=None, station="STA1", minute=MIN):
    params = {"station": station, "minute_utc": minute}
    if seq is not None:
        params["seq"] = seq
    if fragment_no is not None:
        params["fragment_no"] = fragment_no
    return client.get("/trace", params=params)


def events(client, station="STA1", minute=MIN):
    return client.get(
        "/trace/events", params={"station": station, "minute_utc": minute}
    )


def test_trace_unknown_minute_404(client):
    assert trace(client).status_code == 404
    assert events(client).status_code == 404


def test_trace_added_and_revised_are_recorded_with_increasing_seq(client):
    client.post("/fragments", json=frag(2, 1, 30, list(range(30, 60))))
    client.post("/fragments", json=frag(1, 1, 0, list(range(0, 30))))
    client.post("/fragments", json=frag(1, 2, 0, [9] + list(range(1, 30))))

    body = events(client).json()["events"]
    assert [e["kind"] for e in body] == [
        "fragment_added",
        "fragment_added",
        "fragment_revised",
    ]
    seqs = [e["seq"] for e in body]
    assert seqs == sorted(set(seqs))
    assert [e["minute_seq"] for e in body] == [1, 2, 3]
    revised = body[-1]
    assert revised["fragment_no"] == 1
    assert revised["previous_revision"] == 1
    assert revised["revision"] == 2
    assert revised["previous_samples"] == list(range(0, 30))
    assert revised["samples"] == [9] + list(range(1, 30))


def test_trace_latest_reconstructs_revisions_and_conflicts(client):
    client.post("/fragments", json=frag(1, 1, 0, list(range(0, 30))))
    client.post("/fragments", json=frag(2, 1, 30, list(range(30, 60))))
    body = trace(client).json()
    assert body["state_known"] is True
    assert body["assembly"]["complete"] is True
    assert body["assembly"]["gaps"] == []
    assert body["assembly"]["conflicts"] == []
    assert {f["fragment_no"]: f["revision"] for f in body["fragments"]} == {1: 1, 2: 1}


def test_trace_seq_reports_gap_at_that_moment(client):
    client.post("/fragments", json=frag(2, 1, 30, list(range(30, 60))))
    first_seq = events(client).json()["events"][0]["seq"]

    body = trace(client, seq=first_seq).json()
    assert body["resolved_seq"] == first_seq
    assert body["assembly"]["coverage"] == 30
    # samples 0..29 uncovered, and fragment_no 1 has not arrived yet
    assert body["assembly"]["gaps"] == list(range(0, 30))
    assert body["assembly"]["missing_fragment_nos"] == [0, 1]


def test_trace_conflict_at_seq_and_later_fix_does_not_rewrite_it(client):
    client.post("/fragments", json=frag(1, 1, 0, list(range(0, 30))))
    client.post("/fragments", json=frag(2, 1, 30, list(range(30, 60))))
    client.post("/fragments", json=frag(2, 2, 0, [7] + list(range(1, 30))))
    conflict_seq = events(client).json()["events"][-1]["seq"]

    body = trace(client, seq=conflict_seq).json()
    assert body["assembly"]["conflicts"] == [
        {"position": 0, "fragment_nos": [1, 2], "values": [0, 7]}
    ]

    # Correction that eliminates the conflict arrives afterwards.
    client.post("/fragments", json=frag(2, 3, 30, list(range(30, 60))))
    assert trace(client).json()["assembly"]["conflicts"] == []

    # The old sequence keeps its conflict and its old revision.
    again = trace(client, seq=conflict_seq).json()
    assert again["assembly"]["conflicts"] == [
        {"position": 0, "fragment_nos": [1, 2], "values": [0, 7]}
    ]
    frag2 = next(f for f in again["fragments"] if f["fragment_no"] == 2)
    assert frag2["revision"] == 2
    assert frag2["samples"] == [7] + list(range(1, 30))


def test_trace_marks_revision_superseded_later(client):
    client.post("/fragments", json=frag(1, 1, 0, [1, 2, 3]))
    seq1 = events(client).json()["events"][0]["seq"]
    client.post("/fragments", json=frag(1, 2, 0, [4, 5, 6]))
    seq2 = events(client).json()["events"][-1]["seq"]

    before = trace(client, seq=seq1).json()["fragments"][0]
    assert before["revision"] == 1
    assert before["superseded_later"] is True
    assert before["superseded_at_seq"] == seq2

    after = trace(client, seq=seq2).json()["fragments"][0]
    assert after["revision"] == 2
    assert after["superseded_later"] is False
    assert after["superseded_at_seq"] is None


def test_trace_by_fragment_no(client):
    client.post("/fragments", json=frag(1, 1, 0, [1, 2]))
    client.post("/fragments", json=frag(2, 1, 10, [3, 4]))
    client.post("/fragments", json=frag(2, 2, 10, [5, 6]))

    body = trace(client, fragment_no=2).json()
    assert body["event"]["fragment_no"] == 2
    assert body["event"]["revision"] == 2
    assert body["event"]["previous_revision"] == 1

    assert trace(client, fragment_no=99).status_code == 404
    both = client.get(
        "/trace",
        params={"station": "STA1", "minute_utc": MIN, "seq": 1, "fragment_no": 2},
    )
    assert both.status_code == 422


def test_trace_flags_points_to_expired_publications(client):
    client.post("/fragments", json=frag(1, 1, 0, list(range(0, 30))))
    client.post("/fragments", json=frag(2, 1, 30, list(range(30, 60))))
    pub = client.post(
        "/publications", params={"station": "STA1", "minute_utc": MIN}
    ).json()

    client.post("/fragments", json=frag(1, 2, 0, [9] + list(range(1, 30))))
    body = trace(client).json()
    expired = body["expired_publications"]
    assert [p["id"] for p in expired] == [pub["id"]]
    assert expired[0]["invalidated_by_seq"] == body["resolved_seq"]
    assert expired[0]["invalidated_at"]
    assert expired[0]["created_event_seq"] is not None
    # and the publication itself agrees on the cause of its staleness
    assert client.get(f"/publications/{pub['id']}").json()["stale"] is True


def test_trace_publication_fresh_at_its_own_sequence(client):
    client.post("/fragments", json=frag(1, 1, 0, list(range(0, 30))))
    client.post("/fragments", json=frag(2, 1, 30, list(range(30, 60))))
    pub = client.post(
        "/publications", params={"station": "STA1", "minute_utc": MIN}
    ).json()
    publish_seq = next(
        e["seq"] for e in events(client).json()["events"] if e["kind"] == "publish"
    )
    client.post("/fragments", json=frag(1, 2, 0, [9] + list(range(1, 30))))

    at_publish = trace(client, seq=publish_seq).json()
    assert [p["id"] for p in at_publish["publications"]] == [pub["id"]]
    assert at_publish["publications"][0]["fresh"] is True
    assert at_publish["publications"][0]["invalidated_event_seq"] is None
    assert at_publish["expired_publications"] == []

    latest = trace(client).json()
    assert latest["publications"] == []  # its set no longer matches


def test_idempotent_retry_adds_no_trace_event(client):
    client.post("/fragments", json=frag(1, 1, 0, [1, 2, 3]))
    before = events(client).json()["events"]
    r = client.post("/fragments", json=frag(1, 1, 0, [1, 2, 3]))
    assert r.json()["status"] == "unchanged"
    after = events(client).json()["events"]
    assert len(after) == len(before)


def test_rejected_uploads_add_no_trace_event(client):
    client.post("/fragments", json=frag(1, 2, 0, [1, 2, 3]))
    before = events(client).json()["events"]
    assert client.post("/fragments", json=frag(1, 1, 0, [9])).status_code == 409
    assert client.post("/fragments", json=frag(1, 2, 0, [9])).status_code == 409
    assert client.post("/fragments", json=frag(9, 1, 58, [1, 2, 3])).status_code == 422
    assert events(client).json()["events"] == before


def test_failed_publish_adds_no_trace_event(client):
    client.post("/fragments", json=frag(1, 1, 0, [1, 2]))
    before = events(client).json()["events"]
    r = client.post("/publications", params={"station": "STA1", "minute_utc": MIN})
    assert r.status_code == 409
    assert events(client).json()["events"] == before


def test_trace_is_station_scoped(client):
    client.post("/fragments", json=frag(1, 1, 0, [1, 2], station="STA1"))
    client.post("/fragments", json=frag(1, 1, 0, [4, 5], station="STA2"))
    assert trace(client, station="STA1").json()["fragments"][0]["samples"] == [1, 2]
    assert trace(client, station="STA2").json()["fragments"][0]["samples"] == [4, 5]


def test_trace_new_minute_is_known_empty_before_first_event(client):
    # An unrelated minute has history, so the global sequence is advanced.
    other = "2026-11-17T05:07:00Z"
    client.post("/fragments", json=frag(1, 1, 0, [1, 2, 3], minute=other))
    client.post("/fragments", json=frag(1, 1, 0, [4, 5], minute=MIN))
    first_seq = events(client).json()["events"][0]["seq"]

    body = trace(client, seq=first_seq - 1).json()
    assert body["state_known"] is True
    assert body["fragments"] == []
    assert body["assembly"]["coverage"] == 0
    assert body["assembly"]["missing_fragment_nos"] == []
    assert body["publications"] == []


def test_trace_missing_fragment_numbers_vs_sample_gaps(client):
    # Fragment numbering starts at 2: fragment 1 never arrived.
    client.post("/fragments", json=frag(2, 1, 30, list(range(30, 60))))
    body = trace(client).json()
    assert body["assembly"]["missing_fragment_nos"] == [0, 1]
    assert body["assembly"]["gaps"] == list(range(0, 30))


# --------------------------------------------------------------------------- #
# Upgrade from a pre-trace database
# --------------------------------------------------------------------------- #
def _create_legacy_db(path: Path) -> None:
    """Write a database with the *old* schema and data, as v1.0 left it."""
    import sqlite3

    # The client fixture already opened this path; start from a clean file.
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            candidate.unlink()

    legacy_schema = """
    CREATE TABLE fragments (
        id INTEGER PRIMARY KEY AUTOINCREMENT, station TEXT NOT NULL,
        minute_utc TEXT NOT NULL, fragment_no INTEGER NOT NULL,
        revision INTEGER NOT NULL CHECK (revision >= 1),
        start_sample INTEGER NOT NULL CHECK (start_sample >= 0 AND start_sample < 60),
        samples TEXT NOT NULL, content_hash TEXT NOT NULL,
        UNIQUE (station, minute_utc, fragment_no));
    CREATE TABLE publications (
        id INTEGER PRIMARY KEY AUTOINCREMENT, station TEXT NOT NULL,
        minute_utc TEXT NOT NULL, wave TEXT NOT NULL,
        fragment_set_hash TEXT NOT NULL,
        stale INTEGER NOT NULL DEFAULT 0 CHECK (stale IN (0, 1)),
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')));
    CREATE TABLE publication_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        publication_id INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
        fragment_no INTEGER NOT NULL, revision INTEGER NOT NULL,
        start_sample INTEGER NOT NULL, samples TEXT NOT NULL,
        content_hash TEXT NOT NULL);
    """
    from app.assembly import content_hash, dump_samples, fragment_snapshot, snapshot_hash

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(legacy_schema)
    rows = [
        ("LEG", MIN, 1, 1, 0, list(range(0, 30))),
        ("LEG", MIN, 2, 1, 30, list(range(30, 60))),
    ]
    for station, minute, no, rev, start, samples in rows:
        conn.execute(
            "INSERT INTO fragments (station, minute_utc, fragment_no, revision, "
            "start_sample, samples, content_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (station, minute, no, rev, start, dump_samples(samples),
             content_hash(start, samples)),
        )
    snap = fragment_snapshot(
        conn.execute("SELECT * FROM fragments WHERE station='LEG'").fetchall()
    )
    set_hash = snapshot_hash(snap)
    cur = conn.execute(
        "INSERT INTO publications (station, minute_utc, wave, fragment_set_hash, "
        "stale) VALUES ('LEG', ?, ?, ?, 0)",
        (MIN, dump_samples(list(range(60))), set_hash),
    )
    pub_id = cur.lastrowid
    for f in snap:
        conn.execute(
            "INSERT INTO publication_items (publication_id, fragment_no, revision, "
            "start_sample, samples, content_hash) VALUES (?, ?, ?, ?, ?, ?)",
            (pub_id, f["fragment_no"], f["revision"], f["start_sample"],
             dump_samples(f["samples"]), f["content_hash"]),
        )
    # a second minute with only a fragment, no publication
    conn.execute(
        "INSERT INTO fragments (station, minute_utc, fragment_no, revision, "
        "start_sample, samples, content_hash) VALUES ('GAP', ?, 1, 1, 0, ?, ?)",
        (MIN, dump_samples([5, 6, 7]), content_hash(0, [5, 6, 7])),
    )
    conn.commit()
    conn.close()


def test_upgrade_seeds_baseline_and_marks_prehistory_unknown(client):
    db_path = Path(os.environ["WAVEFORM_DB_PATH"])
    client.close()

    import importlib

    from app import config, database, main, service

    _create_legacy_db(db_path)
    importlib.reload(config)
    importlib.reload(database)
    importlib.reload(service)
    importlib.reload(main)
    database.init_db()

    with TestClient(main.app) as c:
        body = trace(c, station="LEG").json()
        assert body["state_known"] is True
        assert body["unknown_before_baseline"] is True
        assert {f["fragment_no"]: f["revision"] for f in body["fragments"]} == {1: 1, 2: 1}
        assert body["assembly"]["complete"] is True
        # pre-existing publication is visible, but its creation time is unknown
        assert len(body["publications"]) == 1
        pub = body["publications"][0]
        assert pub["creation_time_known"] is False
        assert pub["created_event_seq"] is None
        assert pub["created_at"] is None

        # asking before the baseline: explicitly unknown, never guessed
        before = trace(c, seq=0, station="LEG").json()
        assert before["state_known"] is False
        assert before["unknown_reason"]
        assert before["fragments"] == []
        assert before["assembly"] is None
        assert before["publications"] == []

        # a gap-only legacy minute is preserved as such
        gap = trace(c, station="GAP").json()
        assert gap["assembly"]["gaps"] == list(range(3, 60))
        assert gap["assembly"]["missing_fragment_nos"] == [0]

        # a correction after the upgrade expires the legacy publication and is
        # attributed to the new change, not to the unknown past
        c.post("/fragments", json=frag(1, 2, 0, [9] + list(range(1, 30)),
                                       station="LEG"))
        after = trace(c, station="LEG").json()
        assert [p["id"] for p in after["expired_publications"]] == [pub["id"]]
        assert after["expired_publications"][0]["invalidated_by_seq"] == after["resolved_seq"]
        # the pre-correction snapshot is still the baseline state
        old = trace(c, seq=body["resolved_seq"], station="LEG").json()
        assert next(f for f in old["fragments"] if f["fragment_no"] == 1)["revision"] == 1
        assert old["publications"][0]["id"] == pub["id"]
        assert c.get(f"/publications/{pub['id']}").json()["stale"] is True


def test_upgrade_is_idempotent_and_keeps_publication_identity(client):
    db_path = Path(os.environ["WAVEFORM_DB_PATH"])
    client.close()

    import importlib

    from app import config, database, main, service

    _create_legacy_db(db_path)
    importlib.reload(config)
    importlib.reload(database)
    importlib.reload(service)
    importlib.reload(main)
    database.init_db()

    import sqlite3

    conn = sqlite3.connect(str(db_path))
    before_events = conn.execute("SELECT COUNT(*) FROM trace_events").fetchone()[0]
    before_pubs = conn.execute("SELECT COUNT(*) FROM publications").fetchone()[0]
    conn.close()

    database.init_db()  # restart on an already-upgraded database

    conn = sqlite3.connect(str(db_path))
    assert conn.execute("SELECT COUNT(*) FROM trace_events").fetchone()[0] == before_events
    assert conn.execute("SELECT COUNT(*) FROM publications").fetchone()[0] == before_pubs
    assert conn.execute(
        "SELECT COUNT(*) FROM trace_events WHERE kind = 'baseline'"
    ).fetchone()[0] == before_events
    conn.close()


def test_concurrent_uploads_publishes_and_trace_agree(monkeypatch):
    """Uploads, publishes and trace reads interleave on real uvicorn threads.

    Whatever snapshot a trace read returns must be self-consistent: its
    reported ``resolved_seq`` event snapshot equals the reconstructed fragment
    set and assembly, its ``expired_publications`` matches the publications
    actually stale at that instant, and no event is duplicated or skipped.
    """
    import socket
    import time

    import httpx
    import uvicorn

    tmpdir = tempfile.mkdtemp(prefix="wf-trace-live-")
    db_path = Path(tmpdir) / "live.db"
    monkeypatch.setenv("WAVEFORM_DB_PATH", str(db_path))

    import importlib

    from app import config, database, main, service

    importlib.reload(config)
    importlib.reload(database)
    importlib.reload(service)
    importlib.reload(main)
    database.init_db()

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    server = uvicorn.Server(
        uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if httpx.get(base + "/health", timeout=1).status_code == 200:
                break
        except httpx.TransportError:
            time.sleep(0.1)
    else:  # pragma: no cover
        raise RuntimeError("test server did not start")

    errors: list[Exception] = []
    barrier = threading.Barrier(5)

    def worker(idx: int) -> None:
        try:
            no = idx + 1
            barrier.wait()
            r = httpx.post(
                base + "/fragments",
                json=frag(no, 1, (no - 1) * 12, [no * 100] * 12),
                timeout=10,
            )
            assert r.status_code == 201, r.text
            if idx % 2 == 0:
                httpx.post(
                    base + "/fragments",
                    json=frag(no, 2, (no - 1) * 12, [no * 100] * 12),
                    timeout=10,
                )
            rp = httpx.post(
                base + "/publications",
                params={"station": "STA1", "minute_utc": MIN},
                timeout=10,
            )
            assert rp.status_code in (201, 409), rp.text
            rt = httpx.get(
                base + "/trace",
                params={"station": "STA1", "minute_utc": MIN},
                timeout=10,
            )
            assert rt.status_code == 200, rt.text
        except Exception as exc:  # pragma: no cover - failure reporting
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors

    with httpx.Client(base_url=base, timeout=10) as http:
        evs = http.get(
            "/trace/events", params={"station": "STA1", "minute_utc": MIN}
        ).json()["events"]
        seqs = [e["seq"] for e in evs]
        assert seqs == sorted(set(seqs))
        added = [e for e in evs if e["kind"] == "fragment_added"]
        assert len(added) == 5
        assert sorted(e["fragment_no"] for e in added) == [1, 2, 3, 4, 5]

        # Snapshot at every event must equal the assembly implied by its own
        # recorded fragment set: history never mixes two committed states.
        for e in evs:
            if e["kind"] not in ("fragment_added", "fragment_revised"):
                continue
            snap = http.get(
                "/trace",
                params={"station": "STA1", "minute_utc": MIN, "seq": e["seq"]},
            ).json()
            assert snap["resolved_seq"] == e["seq"]
            total = sum(len(f["samples"]) for f in snap["fragments"])
            assert snap["assembly"]["coverage"] <= total
            # expired publications always point at an event at/before now
            for p in snap["expired_publications"]:
                assert p["invalidated_by_seq"] == e["seq"]

        # Final: every publication is stale exactly when a later change hit it.
        pubs = http.get(
            "/publications", params={"station": "STA1", "minute_utc": MIN}
        ).json()
        latest = http.get(
            "/trace", params={"station": "STA1", "minute_utc": MIN}
        ).json()
        fresh_hash = latest["event"]["snapshot_hash"]
        for pub in pubs:
            should_be_stale = pub["fragment_set_hash"] != fresh_hash
            assert pub["stale"] is should_be_stale

    server.should_exit = True
    thread.join(timeout=10)


def test_trace_old_seq_missing_list_not_rewritten_by_later_arrival(client):
    client.post("/fragments", json=frag(2, 1, 30, list(range(30, 60))))
    seq1 = events(client).json()["events"][0]["seq"]
    before = trace(client, seq=seq1).json()["assembly"]["missing_fragment_nos"]
    assert before == [0, 1]

    client.post("/fragments", json=frag(1, 1, 0, list(range(0, 30))))
    after = trace(client, seq=seq1).json()["assembly"]["missing_fragment_nos"]
    assert after == before  # the old result is immutable
    assert trace(client).json()["assembly"]["missing_fragment_nos"] == [0]


def test_self_healed_staleness_is_attributed_consistently(client, monkeypatch):
    """A legacy record whose flag was wrong must expose the same cause in both
    the publication listing and the trace view."""
    client.post("/fragments", json=frag(1, 1, 0, list(range(0, 30))))
    client.post("/fragments", json=frag(2, 1, 30, list(range(30, 60))))
    pub = client.post(
        "/publications", params={"station": "STA1", "minute_utc": MIN}
    ).json()

    # Simulate an old row: fresh flag but a set hash that no longer matches.
    import sqlite3

    db_path = Path(os.environ["WAVEFORM_DB_PATH"])
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE publications SET stale = 0, invalidated_event_seq = NULL "
        "WHERE id = ?",
        (pub["id"],),
    )
    conn.execute(
        "UPDATE publications SET fragment_set_hash = 'deadbeef' WHERE id = ?",
        (pub["id"],),
    )
    conn.commit()
    conn.close()

    assert client.get(f"/publications/{pub['id']}").json()["stale"] is True

    conn = sqlite3.connect(str(db_path))
    cause = conn.execute(
        "SELECT invalidated_event_seq FROM publications WHERE id = ?", (pub["id"],)
    ).fetchone()[0]
    link = conn.execute(
        "SELECT event_seq FROM publication_invalidations WHERE publication_id = ?",
        (pub["id"],),
    ).fetchone()
    conn.close()
    assert cause is not None
    assert link is not None and link[0] == cause

    trace_body = trace(client, seq=cause).json()
    assert [p["id"] for p in trace_body["expired_publications"]] == [pub["id"]]
    assert trace_body["expired_publications"][0]["invalidated_by_seq"] == cause


def test_trace_fresh_database_history_known_from_zero(client):
    other = "2026-11-17T05:07:00Z"
    client.post("/fragments", json=frag(1, 1, 0, [1, 2, 3], minute=other))
    client.post("/fragments", json=frag(1, 1, 0, [4, 5], minute=MIN))
    body = trace(client).json()
    assert body["history_known_from_seq"] == 0
    assert body["unknown_before_baseline"] is False
    assert body["state_known"] is True
