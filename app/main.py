"""HTTP API 入口。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from . import services
from .db import get_db, init_db
from .models import Shift, ShiftResult
from .schemas import (
    AdjudicationAck,
    AdjudicationIn,
    AdjudicationOut,
    ChangeEntry,
    ChangePage,
    DeviceCreate,
    DeviceOut,
    EventAccepted,
    EventBatchAck,
    EventBatchIn,
    EventIn,
    EventItemAck,
    EventVersionRef,
    ReasonMinutes,
    RecomputedShift,
    ResultOut,
    RevisionRef,
    SettleResponse,
    ShiftCreate,
    ShiftOut,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()  # 建库建表（含排他约束），并等待数据库就绪
    yield


app = FastAPI(
    title="设备停机归因服务",
    version="1.3.0",
    description=(
        "按班次对设备停机时间进行归因（保养/故障/待判）。"
        "区间均为半开 [start, end)；重叠时保养 > 故障 > 待判，每分钟只归因一次；"
        "事件按 (来源, 连续版本) 上报，支持幂等与迟到更正重算；"
        "已结算班次支持现场人工裁定（提交/按单号撤销），裁定覆盖自动事件归因，"
        "撤销后回退；仅当各原因分钟数变化时追加班次修订并进入全局游标变更流；"
        "GET /changes 提供按全局递增游标分页的结算/更正/裁定变更流，供下游报表同步。"
    ),
    lifespan=lifespan,
)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    """统一参数校验错误格式（非法区间、缺时区、未对齐整分钟等）。"""
    errors = exc.errors()
    message = "; ".join(
        f"{'.'.join(str(p) for p in e.get('loc', []) if p != 'body')}: {e.get('msg')}"
        .lstrip(": ")
        for e in errors
    )
    return JSONResponse(
        status_code=422,
        content={
            "detail": {
                "code": "VALIDATION_ERROR",
                "message": message or "invalid request",
                "errors": jsonable_encoder(errors, custom_encoder={Exception: str}),
            }
        },
    )


def _result_out(shift: Shift, rev: ShiftResult, latest_revision: int) -> ResultOut:
    return ResultOut(
        shift_id=shift.id,
        device_id=shift.device_id,
        shift_start=shift.start_ts,
        shift_end=shift.end_ts,
        revision=rev.revision,
        latest_revision=latest_revision,
        totals=ReasonMinutes(
            maintenance=rev.maintenance_minutes,
            failure=rev.failure_minutes,
            pending=rev.pending_minutes,
        ),
        delta=ReasonMinutes(
            maintenance=rev.d_maintenance,
            failure=rev.d_failure,
            pending=rev.d_pending,
        ),
        event_versions=[EventVersionRef(**ev) for ev in rev.event_versions],
        trigger=rev.trigger,
        created_at=rev.created_at,
    )


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------- 设备


@app.post("/devices", response_model=DeviceOut, tags=["devices"])
def create_device(
    payload: DeviceCreate, response: Response, db: Session = Depends(get_db)
):
    """注册设备；重复提交同 id 设备幂等返回（200）。"""
    device, created = services.create_device(db, payload)
    response.status_code = 201 if created else 200
    return DeviceOut(id=device.id, name=device.name)


@app.get("/devices", response_model=list[DeviceOut], tags=["devices"])
def list_devices(db: Session = Depends(get_db)):
    return [DeviceOut(id=d.id, name=d.name) for d in services.list_devices(db)]


# ---------------------------------------------------------------- 班次


@app.post("/shifts", response_model=ShiftOut, status_code=201, tags=["shifts"])
def create_shift(payload: ShiftCreate, db: Session = Depends(get_db)):
    """创建班次（半开区间，同设备不可重叠）。"""
    shift = services.create_shift(db, payload)
    return ShiftOut(
        id=shift.id,
        device_id=shift.device_id,
        start=shift.start_ts,
        end=shift.end_ts,
    )


@app.get("/shifts/{shift_id}", response_model=ShiftOut, tags=["shifts"])
def get_shift(shift_id: int, db: Session = Depends(get_db)):
    shift = services.get_shift_or_404(db, shift_id)
    return ShiftOut(
        id=shift.id,
        device_id=shift.device_id,
        start=shift.start_ts,
        end=shift.end_ts,
    )


@app.post(
    "/shifts/{shift_id}/settle", response_model=SettleResponse, tags=["attribution"]
)
def settle_shift(shift_id: int, db: Session = Depends(get_db)):
    """结算班次：按当前生效事件计算归因，写入修订 1；重复调用幂等返回最新修订。"""
    shift, rev, created = services.settle_shift(db, shift_id)
    return SettleResponse(
        settled=created, result=_result_out(shift, rev, rev.revision)
    )


# ---------------------------------------------------------------- 事件


@app.post("/events", response_model=EventAccepted, tags=["events"])
def ingest_event(
    payload: EventIn, response: Response, db: Session = Depends(get_db)
):
    """上报停机事件版本。

    - 同来源同版本同内容：幂等返回（200，idempotent=true）；
    - 同版本异内容 / 跳号：409；
    - 新版本（含迟到更正）：201，并按事件时间重算受影响的已结算班次，
      在 recomputed 中返回新修订与差额。
    """
    ev, idempotent, recomputed = services.ingest_event(db, payload)
    response.status_code = 200 if idempotent else 201
    return EventAccepted(
        device_id=ev.device_id,
        source_id=ev.source_id,
        version=ev.version,
        idempotent=idempotent,
        recomputed=[
            RecomputedShift(
                shift_id=shift.id,
                revision=rev.revision,
                totals=ReasonMinutes(
                    maintenance=rev.maintenance_minutes,
                    failure=rev.failure_minutes,
                    pending=rev.pending_minutes,
                ),
                delta=ReasonMinutes(
                    maintenance=rev.d_maintenance,
                    failure=rev.d_failure,
                    pending=rev.d_pending,
                ),
            )
            for shift, rev in recomputed
        ],
    )


@app.post("/events/batch", response_model=EventBatchAck, tags=["events"])
def ingest_event_batch(
    payload: EventBatchIn, response: Response, db: Session = Depends(get_db)
):
    """现场网关断网恢复后的一次性补传：同一设备的非空事件列表。

    - 各项沿用单条上报的来源、版本、起止、原因字段与校验；
      按列表顺序核对每个来源的版本链：
      已存在的同版本同内容重试幂等（item.status="idempotent"），
      同版本异内容 409 VERSION_CONFLICT、跳号 409 VERSION_GAP、
      未知设备 404 DEVICE_NOT_FOUND、任一项字段非法 422；
    - 任一单项失败 → 整批失败，不留下事件、来源游标推进、修订或变更；
    - 成功时先应用全部新版本，再以批前与批后各来源最新版本区间的并集
      确定受影响的已结算班次，按最终状态重算：每个班次最多产生一次修订
      与一条变更，原因分钟数未变则不产生；
    - 与单条上报及并发批次共用来源行锁，连续版本链不被破坏。
    """
    device_id, events, idempotents, recomputed = services.ingest_event_batch(
        db, payload
    )
    response.status_code = 200 if events and all(idempotents) else 201
    return EventBatchAck(
        device_id=device_id,
        status="applied",
        items=[
            EventItemAck(
                index=i,
                source_id=ev.source_id,
                version=ev.version,
                status="idempotent" if idempotents[i] else "created",
                idempotent=idempotents[i],
            )
            for i, ev in enumerate(events)
        ],
        recomputed=[
            RecomputedShift(
                shift_id=shift.id,
                revision=rev.revision,
                totals=ReasonMinutes(
                    maintenance=rev.maintenance_minutes,
                    failure=rev.failure_minutes,
                    pending=rev.pending_minutes,
                ),
                delta=ReasonMinutes(
                    maintenance=rev.d_maintenance,
                    failure=rev.d_failure,
                    pending=rev.d_pending,
                ),
            )
            for shift, rev in recomputed
        ],
    )


# ---------------------------------------------------------------- 结果查询


@app.get("/shifts/{shift_id}/result", response_model=ResultOut, tags=["attribution"])
def get_result(
    shift_id: int,
    revision: int | None = Query(default=None, ge=1, description="指定修订号，缺省为最新"),
    db: Session = Depends(get_db),
):
    """查询班次归因结果；可用 ?revision=N 查看历史修订（原结果永久保留）。"""
    shift, rev, latest_revision = services.get_result(db, shift_id, revision)
    return _result_out(shift, rev, latest_revision)


@app.get(
    "/shifts/{shift_id}/revisions", response_model=list[ResultOut], tags=["attribution"]
)
def list_revisions(shift_id: int, db: Session = Depends(get_db)):
    """列出班次的全部修订（含每次更正的差额）。"""
    shift, revs = services.list_revisions(db, shift_id)
    latest_revision = revs[-1].revision if revs else 0
    return [_result_out(shift, rev, latest_revision) for rev in revs]


# ---------------------------------------------------------------- 现场人工裁定


def _adjudication_ack(
    ruling, idempotent: bool, rev: ShiftResult | None, latest_revision: int
) -> AdjudicationAck:
    detail = None
    if rev is not None:
        detail = RevisionRef(
            revision=rev.revision,
            seq=rev.seq,
            totals=ReasonMinutes(
                maintenance=rev.maintenance_minutes,
                failure=rev.failure_minutes,
                pending=rev.pending_minutes,
            ),
            delta=ReasonMinutes(
                maintenance=rev.d_maintenance,
                failure=rev.d_failure,
                pending=rev.d_pending,
            ),
            trigger=rev.trigger,
        )
    return AdjudicationAck(
        ruling_no=ruling.ruling_no,
        status=ruling.status,
        idempotent=idempotent,
        revision=rev.revision if rev is not None else None,
        latest_revision=latest_revision,
        revision_detail=detail,
    )


@app.post(
    "/shifts/{shift_id}/adjudications",
    response_model=AdjudicationAck,
    tags=["adjudications"],
)
def submit_adjudication(
    shift_id: int,
    payload: AdjudicationIn,
    response: Response,
    db: Session = Depends(get_db),
):
    """为已结算班次提交现场人工裁定。

    - 裁定分钟覆盖自动事件归因；重叠分钟按生效顺序最新的未撤销裁定为准，
      撤销后回退至此前有效裁定，无裁定时按当前最新事件版本计算；
    - 同单号同内容重试幂等（200，不产生修订）；同单号异内容 409 RULING_CONFLICT；
    - 仅当各原因分钟数变化时产生班次修订（revision 非空）并进入 /changes；
    - 未知班次 404、班次未结算 409 SHIFT_NOT_SETTLED、区间越界 422 RULING_OUT_OF_RANGE、
      非法原因/时间格式 422 VALIDATION_ERROR。
    """
    _shift, ruling, idempotent, rev, latest = services.submit_adjudication(
        db, shift_id, payload
    )
    response.status_code = 200 if idempotent else 201
    return _adjudication_ack(ruling, idempotent, rev, latest)


@app.post(
    "/shifts/{shift_id}/adjudications/{ruling_no}/revoke",
    response_model=AdjudicationAck,
    tags=["adjudications"],
)
def revoke_adjudication(
    shift_id: int,
    ruling_no: str,
    response: Response,
    db: Session = Depends(get_db),
):
    """按单号撤销现场人工裁定。

    - 首次撤销 201，分钟数变化时产生修订（trigger=adjudication_revoke）；
    - 重复撤销幂等（200，不产生修订）；
    - 未知班次 404 SHIFT_NOT_FOUND，未知单号 404 RULING_NOT_FOUND。
    """
    _shift, ruling, idempotent, rev, latest = services.revoke_adjudication(
        db, shift_id, ruling_no
    )
    response.status_code = 200 if idempotent else 201
    return _adjudication_ack(ruling, idempotent, rev, latest)


@app.get(
    "/shifts/{shift_id}/adjudications",
    response_model=list[AdjudicationOut],
    tags=["adjudications"],
)
def list_adjudications(shift_id: int, db: Session = Depends(get_db)):
    """列出班次的全部现场人工裁定（含已撤销），按生效顺序排列。"""
    _shift, rows = services.list_adjudications(db, shift_id)
    return [
        AdjudicationOut(
            ruling_no=r.ruling_no,
            start=r.start_ts,
            end=r.end_ts,
            reason=r.reason,
            note=r.note,
            status=r.status,
            created_at=r.created_at,
            revoked_at=r.revoked_at,
        )
        for r in rows
    ]


# ---------------------------------------------------------------- 变更同步


def _change_entry(shift: Shift, rev: ShiftResult) -> ChangeEntry:
    return ChangeEntry(
        seq=rev.seq,
        shift_id=shift.id,
        device_id=shift.device_id,
        shift_start=shift.start_ts,
        shift_end=shift.end_ts,
        revision=rev.revision,
        totals=ReasonMinutes(
            maintenance=rev.maintenance_minutes,
            failure=rev.failure_minutes,
            pending=rev.pending_minutes,
        ),
        delta=ReasonMinutes(
            maintenance=rev.d_maintenance,
            failure=rev.d_failure,
            pending=rev.d_pending,
        ),
        event_versions=[EventVersionRef(**ev) for ev in rev.event_versions],
        trigger=rev.trigger,
        created_at=rev.created_at,
    )


@app.get("/changes", response_model=ChangePage, tags=["sync"])
def list_changes(
    cursor: int = Query(
        default=0, ge=0, description="已消费的最大变更序号；0（缺省）表示从头回放"
    ),
    limit: int = Query(
        default=100,
        ge=1,
        le=services.MAX_PAGE_SIZE,
        description=f"每页条数，上限 {services.MAX_PAGE_SIZE}",
    ),
    db: Session = Depends(get_db),
):
    """下游报表同步：按全局递增游标分页读取每次结算/更正产生的变更。

    - 每条含班次、修订号、各原因分钟数、差额与触发类型；
    - 修订与变更记录同事务提交/回滚；序号顺序即提交顺序，
      并发结算/更正时游标不会越过尚未可见的较早变更；
    - 已返回条目不可变，重读内容不变；空页保持原游标；
    - 非法游标（负数、超过当前最大序号）或超限页大小返回 422。
    """
    page, next_cursor = services.list_changes(db, cursor, limit)
    return ChangePage(
        changes=[_change_entry(shift, rev) for rev, shift in page],
        next_cursor=next_cursor,
    )
