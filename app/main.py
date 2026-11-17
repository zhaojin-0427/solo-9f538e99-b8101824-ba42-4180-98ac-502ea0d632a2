"""FastAPI application and HTTP routes."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from . import service
from .database import init_db
from .models import (
    AssemblyOut,
    FragmentIn,
    FragmentOut,
    PublicationOut,
    TraceEventListOut,
    TraceOut,
)


TRACE_DESCRIPTION = """
### 拼接质量追溯（Traceability）

当片段被接受（新增片段、或更高修订覆盖旧修订）时，服务在**同一个事务**内追加一条
不可变事件：全局递增序号 `seq`、变更后的完整有效片段集合、以及被本次变更置为过期的发布。

- `GET /trace?station=&minute_utc=&seq=` —— 重建该分钟在 `seq` 时刻（含）的片段修订集合、
  缺位（样本位与片段号）与冲突，并列出**这一次变更**使哪些发布过期。
  * `seq` 省略时取最近一次已接受的片段变更；`seq` 小于历史起点时返回
    `state_known=false`，明确标注「未知」，不会猜测。
- `GET /trace?station=&minute_utc=&fragment_no=` —— 定位某片段的最近一次变更快照。
- `GET /trace/events?station=&minute_utc=` —— 该分钟完整、按序的变化账本。

历史快照一经写入不会因后续更正而改写：后续更正只会新增事件，
并让旧序号的结果继续指向旧修订。
"""


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Seismic Waveform Assembly API",
    version="1.1.0",
    description=(
        "Ingest out-of-order waveform fragments, assemble them into whole "
        "UTC-minute records and publish immutable minutes for phase pickers.\n\n"
        "Includes an append-only quality-trace ledger so quality control can "
        "reconstruct, for any sequence, which fragment revisions were effective, "
        "where gaps/conflicts were, and which publications a change expired.\n\n"
        + TRACE_DESCRIPTION
    ),
    lifespan=lifespan,
)


@app.exception_handler(service.ServiceError)
async def _service_error_handler(_: Request, exc: service.ServiceError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": str(exc)},
    )


@app.exception_handler(LookupError)
async def _lookup_error_handler(_: Request, exc: LookupError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


class UploadOut(FragmentOut):
    status: str


@app.get("/health", tags=["ops"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/fragments", response_model=UploadOut, status_code=201, tags=["fragments"])
def upload_fragment(frag: FragmentIn) -> dict[str, Any]:
    return service.upload_fragment(frag.model_dump())


@app.get("/fragments", response_model=list[FragmentOut], tags=["fragments"])
def list_fragments(
    station: str = Query(min_length=1),
    minute_utc: datetime = Query(description="UTC minute, e.g. 2026-11-17T05:06:00Z"),
) -> list[dict[str, Any]]:
    return service.list_fragments(station, minute_utc)


@app.get("/assembly", response_model=AssemblyOut, tags=["fragments"])
def get_assembly(
    station: str = Query(min_length=1),
    minute_utc: datetime = Query(description="UTC minute, e.g. 2026-11-17T05:06:00Z"),
) -> dict[str, Any]:
    return service.get_assembly(station, minute_utc)


@app.post("/publications", response_model=PublicationOut, status_code=201, tags=["publications"])
def publish_minute(
    station: str = Query(min_length=1),
    minute_utc: datetime = Query(description="UTC minute, e.g. 2026-11-17T05:06:00Z"),
) -> dict[str, Any]:
    return service.publish_minute(station, minute_utc)


@app.get("/publications", response_model=list[PublicationOut], tags=["publications"])
def list_publications(
    station: str | None = Query(default=None),
    minute_utc: datetime | None = Query(default=None),
) -> list[dict[str, Any]]:
    return service.list_publications(station, minute_utc)


@app.get("/publications/latest", response_model=PublicationOut, tags=["publications"])
def get_latest_publication(
    station: str = Query(min_length=1),
    minute_utc: datetime = Query(...),
) -> JSONResponse | dict[str, Any]:
    result = service.get_latest_publication(station, minute_utc)
    if result is None:
        return JSONResponse(status_code=404, content={"detail": "publication not found"})
    return result


@app.get("/publications/{pub_id}", response_model=PublicationOut, tags=["publications"])
def get_publication(pub_id: int) -> JSONResponse | dict[str, Any]:
    result = service.get_publication(pub_id)
    if result is None:
        return JSONResponse(status_code=404, content={"detail": "publication not found"})
    return result


@app.get("/trace", response_model=TraceOut, tags=["trace"])
def trace_minute(
    station: str = Query(min_length=1, description="Station code"),
    minute_utc: datetime = Query(description="UTC minute, e.g. 2026-11-17T05:06:00Z"),
    seq: int | None = Query(
        default=None,
        ge=0,
        description=(
            "Global change sequence to reconstruct (state at the greatest "
            "committed event with seq <= this value). Omit for the latest "
            "accepted fragment change."
        ),
    ),
    fragment_no: int | None = Query(
        default=None,
        ge=0,
        description="Instead of seq: locate the latest accepted change of this fragment.",
    ),
) -> dict[str, Any]:
    return service.trace_minute(station, minute_utc, seq=seq, fragment_no=fragment_no)


@app.get("/trace/events", response_model=TraceEventListOut, tags=["trace"])
def list_trace_events(
    station: str = Query(min_length=1),
    minute_utc: datetime = Query(description="UTC minute, e.g. 2026-11-17T05:06:00Z"),
) -> dict[str, Any]:
    return service.list_trace_events(station, minute_utc)
