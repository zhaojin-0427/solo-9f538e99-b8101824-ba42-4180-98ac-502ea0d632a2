"""Pydantic request/response models."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class FragmentIn(BaseModel):
    station: str = Field(min_length=1, max_length=64)
    minute_utc: datetime
    fragment_no: int = Field(ge=0, le=9999)
    revision: int = Field(ge=1)
    start_sample: int = Field(ge=0, le=59)
    samples: list[int] = Field(min_length=1, max_length=60)

    @field_validator("station")
    @classmethod
    def _station_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("station must not be blank")
        return v

    @field_validator("minute_utc")
    @classmethod
    def _whole_minute(cls, v: datetime) -> datetime:
        # Reject sub-minute precision: every fragment belongs to a whole minute.
        if v.second != 0 or v.microsecond != 0:
            raise ValueError("minute_utc must be a whole minute (seconds=0)")
        return v


class FragmentOut(BaseModel):
    station: str
    minute_utc: datetime
    fragment_no: int
    revision: int
    start_sample: int
    samples: list[int]
    content_hash: str


class ConflictOut(BaseModel):
    position: int
    fragment_nos: list[int]
    values: list[int]


class AssemblyOut(BaseModel):
    station: str
    minute_utc: datetime
    complete: bool
    coverage: int
    gaps: list[int]
    conflicts: list[ConflictOut]
    fragments: list[FragmentOut]


class PublicationItemOut(BaseModel):
    fragment_no: int
    revision: int
    start_sample: int
    samples: list[int]
    content_hash: str


class PublicationOut(BaseModel):
    id: int
    station: str
    minute_utc: datetime
    wave: list[int]
    stale: bool
    fragment_set_hash: str
    created_at: str
    items: list[PublicationItemOut]


# --------------------------------------------------------------------------- #
# Traceability (拼接质量追溯)
# --------------------------------------------------------------------------- #
class TraceFragmentOut(BaseModel):
    """A fragment revision that was effective at the requested sequence."""

    fragment_no: int
    revision: int
    start_sample: int
    samples: list[int]
    content_hash: str
    # Additive metadata: a later correction superseded this revision.  It does
    # not alter the reconstructed state at the requested sequence.
    superseded_later: bool
    superseded_at_seq: int | None


class TraceAssemblyOut(BaseModel):
    complete: bool
    coverage: int
    gaps: list[int]
    conflicts: list[ConflictOut]
    # Holes in the observed fragment numbering (not sample positions).
    missing_fragment_nos: list[int]


class TraceEventOut(BaseModel):
    seq: int
    minute_seq: int
    kind: str
    recorded_at: str
    fragment_no: int | None
    revision: int | None
    previous_revision: int | None
    start_sample: int | None
    samples: list[int] | None
    content_hash: str | None
    previous_start_sample: int | None
    previous_samples: list[int] | None
    previous_content_hash: str | None
    snapshot_hash: str
    fragment_added: bool
    unknown_before: bool
    publication_id: int | None


class TracePublicationOut(BaseModel):
    """A publication as it stood at the requested sequence."""

    id: int
    fragment_set_hash: str
    created_event_seq: int | None
    created_at: str | None
    creation_time_known: bool
    fresh: bool
    invalidated_event_seq: int | None
    invalidated_at: str | None


class ExpiredPublicationOut(BaseModel):
    """A publication that the selected change turned stale."""

    id: int
    fragment_set_hash: str
    created_event_seq: int | None
    created_at: str | None
    creation_time_known: bool
    invalidated_by_seq: int
    invalidated_at: str


class TraceOut(BaseModel):
    station: str
    minute_utc: datetime
    requested_seq: int | None
    resolved_seq: int
    state_known: bool
    unknown_reason: str | None
    history_known_from_seq: int | None
    unknown_before_baseline: bool
    event: TraceEventOut | None
    fragments: list[TraceFragmentOut]
    assembly: TraceAssemblyOut | None
    publications: list[TracePublicationOut]
    expired_publications: list[ExpiredPublicationOut]
    next_seq: int | None
    events_total: int


class TraceEventListOut(BaseModel):
    station: str
    minute_utc: datetime
    events: list[TraceEventOut]
    next_seq: int | None
