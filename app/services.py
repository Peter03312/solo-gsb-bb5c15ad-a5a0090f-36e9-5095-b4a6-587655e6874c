"""业务逻辑：设备、班次、事件版本链、结算与迟到更正重算。"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import attribution
from .attribution import EventView, RulingView
from .models import (
    ChangeCounter,
    Device,
    EventSource,
    EventVersion,
    ManualAdjudication,
    Shift,
    ShiftResult,
)
from .schemas import (
    AdjudicationIn,
    DeviceCreate,
    EventBatchIn,
    EventIn,
    ShiftCreate,
)


class ApiError(HTTPException):
    """统一错误格式：{"detail": {"code": ..., "message": ...}}"""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(
            status_code=status_code, detail={"code": code, "message": message}
        )


def _utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc)


def _content_hash(start: datetime, end: datetime, reason: str) -> str:
    raw = f"{start.isoformat()}|{end.isoformat()}|{reason}"
    return hashlib.sha256(raw.encode()).hexdigest()


def get_device_or_404(db: Session, device_id: str) -> Device:
    device = db.get(Device, device_id)
    if device is None:
        raise ApiError(404, "DEVICE_NOT_FOUND", f"unknown device: {device_id!r}")
    return device


# ---------------------------------------------------------------- 设备


def create_device(db: Session, payload: DeviceCreate) -> tuple[Device, bool]:
    existing = db.get(Device, payload.id)
    if existing is not None:
        return existing, False
    device = Device(id=payload.id, name=payload.name)
    db.add(device)
    try:
        db.commit()
    except IntegrityError:  # 并发重复创建
        db.rollback()
        return db.get(Device, payload.id), False
    return device, True


def list_devices(db: Session) -> list[Device]:
    return list(db.execute(select(Device).order_by(Device.id)).scalars().all())


# ---------------------------------------------------------------- 班次


def create_shift(db: Session, payload: ShiftCreate) -> Shift:
    get_device_or_404(db, payload.device_id)
    start, end = _utc(payload.start), _utc(payload.end)
    conflict = (
        db.execute(
            select(Shift).where(
                Shift.device_id == payload.device_id,
                Shift.start_ts < end,
                Shift.end_ts > start,
            )
        )
        .scalars()
        .first()
    )
    if conflict is not None:
        raise ApiError(
            409,
            "SHIFT_OVERLAP",
            f"shift overlaps existing shift id={conflict.id} "
            f"[{conflict.start_ts.isoformat()}, {conflict.end_ts.isoformat()}) "
            f"on device {payload.device_id!r}",
        )
    shift = Shift(device_id=payload.device_id, start_ts=start, end_ts=end)
    db.add(shift)
    try:
        db.commit()
    except IntegrityError:  # 排他约束兜底（并发创建）
        db.rollback()
        raise ApiError(
            409, "SHIFT_OVERLAP", "shift overlaps an existing shift on this device"
        )
    db.refresh(shift)
    return shift


def get_shift_or_404(db: Session, shift_id: int) -> Shift:
    shift = db.get(Shift, shift_id)
    if shift is None:
        raise ApiError(404, "SHIFT_NOT_FOUND", f"unknown shift id={shift_id}")
    return shift


# ---------------------------------------------------------------- 归因计算


def _allocate_seq(db: Session) -> int:
    """分配全局递增的变更序号。

    单例计数器行在写事务内 UPDATE ... RETURNING 递增，行锁持有至提交：
    序号顺序 == 提交顺序，同步游标因此不会越过尚未可见的较早变更；
    序号与修订在同一事务写入，同时提交或同时回滚。
    """
    db.execute(
        pg_insert(ChangeCounter).values(id=1, last_seq=0).on_conflict_do_nothing()
    )
    return db.execute(
        update(ChangeCounter)
        .where(ChangeCounter.id == 1)
        .values(last_seq=ChangeCounter.last_seq + 1)
        .returning(ChangeCounter.last_seq)
    ).scalar_one()


def _current_event_views(
    db: Session, device_id: str, start: datetime, end: datetime
) -> list[EventView]:
    """设备当前生效事件（每个来源取最新版本）中与窗口 [start, end) 重叠的部分。"""
    stmt = (
        select(EventVersion)
        .join(
            EventSource,
            and_(
                EventVersion.device_id == EventSource.device_id,
                EventVersion.source_id == EventSource.source_id,
                EventVersion.version == EventSource.last_version,
            ),
        )
        .where(
            EventVersion.device_id == device_id,
            EventVersion.start_ts < end,
            EventVersion.end_ts > start,
        )
    )
    rows = db.execute(stmt).scalars().all()
    return [
        EventView(
            source_id=r.source_id,
            version=r.version,
            reason=r.reason,
            start=r.start_ts,
            end=r.end_ts,
        )
        for r in rows
    ]


def _latest_revision(db: Session, shift_id: int) -> ShiftResult | None:
    return (
        db.execute(
            select(ShiftResult)
            .where(ShiftResult.shift_id == shift_id)
            .order_by(ShiftResult.revision.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )


def _compute(db: Session, shift: Shift) -> tuple[dict, list[dict]]:
    events = _current_event_views(db, shift.device_id, shift.start_ts, shift.end_ts)
    rulings = _active_ruling_views(db, shift.id)
    return attribution.attribute_with_rulings(
        shift.start_ts, shift.end_ts, events, rulings
    )


def _active_ruling_views(db: Session, shift_id: int) -> list[RulingView]:
    """班次当前生效（未撤销）的现场人工裁定；id 即生效顺序。"""
    rows = (
        db.execute(
            select(ManualAdjudication)
            .where(
                ManualAdjudication.shift_id == shift_id,
                ManualAdjudication.status == "active",
            )
            .order_by(ManualAdjudication.id)
        )
        .scalars()
        .all()
    )
    return [
        RulingView(
            ruling_no=r.ruling_no,
            reason=r.reason,
            start=r.start_ts,
            end=r.end_ts,
            seq=r.id,
        )
        for r in rows
    ]


def _new_revision(
    db: Session,
    shift: Shift,
    totals: dict,
    used: list[dict],
    trigger: str,
    base: ShiftResult | None,
) -> ShiftResult:
    if base is None:
        revision, bm, bf, bp = 1, 0, 0, 0
    else:
        revision = base.revision + 1
        bm, bf, bp = (
            base.maintenance_minutes,
            base.failure_minutes,
            base.pending_minutes,
        )
    rev = ShiftResult(
        shift_id=shift.id,
        revision=revision,
        seq=_allocate_seq(db),
        maintenance_minutes=totals["maintenance"],
        failure_minutes=totals["failure"],
        pending_minutes=totals["pending"],
        d_maintenance=totals["maintenance"] - bm,
        d_failure=totals["failure"] - bf,
        d_pending=totals["pending"] - bp,
        event_versions=used,
        trigger=trigger,
    )
    db.add(rev)
    return rev


# ---------------------------------------------------------------- 结算


def settle_shift(db: Session, shift_id: int) -> tuple[Shift, ShiftResult, bool]:
    """结算班次：计算并写入修订 1；已结算则幂等返回最新修订。"""
    shift = db.execute(
        select(Shift).where(Shift.id == shift_id).with_for_update()
    ).scalar_one_or_none()
    if shift is None:
        raise ApiError(404, "SHIFT_NOT_FOUND", f"unknown shift id={shift_id}")
    latest = _latest_revision(db, shift_id)
    if latest is not None:
        return shift, latest, False
    totals, used = _compute(db, shift)
    rev = _new_revision(db, shift, totals, used, trigger="settle", base=None)
    db.commit()
    db.refresh(rev)
    return shift, rev, True


# ---------------------------------------------------------------- 事件上报


def ingest_event(
    db: Session, payload: EventIn
) -> tuple[EventVersion, bool, list[tuple[Shift, ShiftResult]]]:
    """上报事件版本。

    返回 (事件版本, 是否幂等命中, 因更正而重算的 [(班次, 新修订)])。
    """
    get_device_or_404(db, payload.device_id)
    start, end = _utc(payload.start), _utc(payload.end)
    digest = _content_hash(start, end, payload.reason)

    # 确保版本链游标存在并加行锁，串行化同一来源的并发上报
    db.execute(
        pg_insert(EventSource)
        .values(
            device_id=payload.device_id, source_id=payload.source_id, last_version=0
        )
        .on_conflict_do_nothing()
    )
    src = db.execute(
        select(EventSource)
        .where(
            EventSource.device_id == payload.device_id,
            EventSource.source_id == payload.source_id,
        )
        .with_for_update()
    ).scalar_one()

    if payload.version <= src.last_version:
        existing = db.execute(
            select(EventVersion).where(
                EventVersion.device_id == payload.device_id,
                EventVersion.source_id == payload.source_id,
                EventVersion.version == payload.version,
            )
        ).scalar_one()
        if existing.content_hash == digest:
            db.commit()
            return existing, True, []  # 同来源同版本同内容：幂等返回
        raise ApiError(
            409,
            "VERSION_CONFLICT",
            f"version {payload.version} of source {payload.source_id!r} "
            "already exists with different content",
        )
    if payload.version > src.last_version + 1:
        raise ApiError(
            409,
            "VERSION_GAP",
            f"source {payload.source_id!r} expects version "
            f"{src.last_version + 1}, got {payload.version}",
        )

    prev = None
    if src.last_version > 0:
        prev = db.execute(
            select(EventVersion).where(
                EventVersion.device_id == payload.device_id,
                EventVersion.source_id == payload.source_id,
                EventVersion.version == src.last_version,
            )
        ).scalar_one()

    ev = EventVersion(
        device_id=payload.device_id,
        source_id=payload.source_id,
        version=payload.version,
        start_ts=start,
        end_ts=end,
        reason=payload.reason,
        content_hash=digest,
    )
    db.add(ev)
    src.last_version = payload.version
    src.last_content_hash = digest
    db.flush()

    recomputed = _recompute_in_windows(
        db,
        payload.device_id,
        [
            (ev.start_ts, ev.end_ts),
            *([(prev.start_ts, prev.end_ts)] if prev is not None else []),
        ],
    )
    db.commit()
    return ev, False, recomputed


def _recompute_in_windows(
    db: Session,
    device_id: str,
    windows: list[tuple[datetime, datetime]],
) -> list[tuple[Shift, ShiftResult]]:
    """按给定半开区间的并集找出受影响的已结算班次，用最新事件版本重算；
    结果有变化时每个班次只追加一次新修订并记录差额。
    """
    conds = [and_(Shift.start_ts < end, Shift.end_ts > start) for start, end in windows]
    shifts = (
        db.execute(
            select(Shift)
            .where(Shift.device_id == device_id, or_(*conds))
            .order_by(Shift.id)  # 固定加锁顺序，降低并发更正相互死锁的概率
            .with_for_update()
        )
        .scalars()
        .all()
    )

    recomputed: list[tuple[Shift, ShiftResult]] = []
    for shift in shifts:
        latest = _latest_revision(db, shift.id)
        if latest is None:
            continue  # 尚未结算：结算时自然会使用最新事件版本
        totals, used = _compute(db, shift)
        if (
            totals["maintenance"] == latest.maintenance_minutes
            and totals["failure"] == latest.failure_minutes
            and totals["pending"] == latest.pending_minutes
        ):
            continue  # 结果未变化，不产生空修订
        rev = _new_revision(db, shift, totals, used, trigger="correction", base=latest)
        db.flush()
        recomputed.append((shift, rev))
    return recomputed


# ---------------------------------------------------------------- 批量补传


def ingest_event_batch(
    db: Session, payload: EventBatchIn
) -> tuple[str, list[EventVersion], list[bool], list[tuple[Shift, ShiftResult]]]:
    """现场网关断网后的一次性补传（同一设备的非空事件列表）。

    按列表顺序逐项核对每个来源的版本链，规则与单条上报完全一致：
    - 已存在的同版本同内容：幂等（status=idempotent，不重写任何数据）；
    - 同版本异内容 → 409 VERSION_CONFLICT；跳号 → 409 VERSION_GAP；
    - 未知设备 → 404 DEVICE_NOT_FOUND；字段非法在路由层 422。
    任何一项失败整批回滚：不留下事件、来源游标推进、修订或变更。

    全部新版本先落库后，以"批前与批后各来源最新版本区间的并集"确定受影响的
    已结算班次，按最终状态重算；每个班次最多产生一次修订与一条变更，
    各原因分钟数未变则不产生。

    返回 (设备 id, 全部事件版本（含幂等命中的既有版本，按列表顺序）,
    逐项是否幂等, 实际产生新修订的 [(班次, 新修订)])。
    """
    get_device_or_404(db, payload.device_id)
    device_id = payload.device_id

    # 确保批中涉及的每个来源的版本链游标存在
    source_ids = list(dict.fromkeys(item.source_id for item in payload.events))
    for source_id in source_ids:
        db.execute(
            pg_insert(EventSource)
            .values(device_id=device_id, source_id=source_id, last_version=0)
            .on_conflict_do_nothing()
        )

    # 按固定顺序（source_id 升序）逐个加锁所有涉及的来源行；
    # 与单条上报共用来源行锁，故并发批次 / 单条上报在同一来源上严格串行，
    # 版本链始终连续；统一加锁顺序避免并发批次相互死锁。
    sources: dict[tuple[str, str], EventSource] = {}
    for source_id in sorted(source_ids):
        sources[(device_id, source_id)] = db.execute(
            select(EventSource)
            .where(
                EventSource.device_id == device_id,
                EventSource.source_id == source_id,
            )
            .with_for_update()
        ).scalar_one()

    # 批前各来源最新版本（用于确定受影响班次的"旧区间"）
    pre_state: dict[str, tuple[int, datetime, datetime] | None] = {}
    for source_id in source_ids:
        src = sources[(device_id, source_id)]
        if src.last_version > 0:
            pre = db.execute(
                select(EventVersion).where(
                    EventVersion.device_id == device_id,
                    EventVersion.source_id == source_id,
                    EventVersion.version == src.last_version,
                )
            ).scalar_one()
            pre_state[source_id] = (
                pre.version,
                pre.start_ts,
                pre.end_ts,
            )
        else:
            pre_state[source_id] = None

    # 按列表顺序核对并应用全部项；批内同来源多次出现时游标沿列表推进。
    # 当前版本链游标（来源 -> 当前 last_version / 最新事件行），随批内写入更新
    cursor: dict[str, int] = {sid: sources[(device_id, sid)].last_version for sid in source_ids}
    newest: dict[str, EventVersion] = {}
    for source_id in source_ids:
        last_ver = cursor[source_id]
        if last_ver > 0:
            newest[source_id] = db.execute(
                select(EventVersion).where(
                    EventVersion.device_id == device_id,
                    EventVersion.source_id == source_id,
                    EventVersion.version == last_ver,
                )
            ).scalar_one()

    events: list[EventVersion] = []
    idempotents: list[bool] = []
    for item in payload.events:
        start, end = _utc(item.start), _utc(item.end)
        digest = _content_hash(start, end, item.reason)
        src = sources[(device_id, item.source_id)]
        cur_ver = cursor[item.source_id]

        if item.version <= cur_ver:
            existing = db.execute(
                select(EventVersion).where(
                    EventVersion.device_id == device_id,
                    EventVersion.source_id == item.source_id,
                    EventVersion.version == item.version,
                )
            ).scalar_one()
            if existing.content_hash != digest:
                raise ApiError(
                    409,
                    "VERSION_CONFLICT",
                    f"version {item.version} of source {item.source_id!r} "
                    "already exists with different content",
                )
            events.append(existing)
            idempotents.append(True)
            # 幂等项不改变游标；但同来源全部项均为旧版本幂等时，
            # newest 仍需指向该来源当前最新版本（已在上方预取，这里兜底）。
            cur_newest = newest.get(item.source_id)
            if cur_newest is None or existing.version > cur_newest.version:
                newest[item.source_id] = existing
            continue

        if item.version > cur_ver + 1:
            raise ApiError(
                409,
                "VERSION_GAP",
                f"source {item.source_id!r} expects version "
                f"{cur_ver + 1}, got {item.version}",
            )

        ev = EventVersion(
            device_id=device_id,
            source_id=item.source_id,
            version=item.version,
            start_ts=start,
            end_ts=end,
            reason=item.reason,
            content_hash=digest,
        )
        db.add(ev)
        db.flush()
        src.last_version = item.version
        src.last_content_hash = digest
        cursor[item.source_id] = item.version
        newest[item.source_id] = ev
        events.append(ev)
        idempotents.append(False)

    # 批后各来源最新版本区间 ∪ 批前最新版本区间 → 受影响班次的判定窗口
    windows: list[tuple[datetime, datetime]] = []
    for source_id in source_ids:
        before = pre_state[source_id]
        if before is not None:
            windows.append((before[1], before[2]))
        after = newest.get(source_id)
        if after is not None and (before is None or after.version != before[0]):
            # 该来源批后最新版本确实被本批推进时才加入新区间
            windows.append((after.start_ts, after.end_ts))

    # flush 来源游标推进，保证下面重算时数据库中 last_version 已是批后值
    db.flush()
    recomputed = _recompute_in_windows(db, device_id, windows)
    db.commit()
    return device_id, events, idempotents, recomputed


# ---------------------------------------------------------------- 现场人工裁定


def _ruling_hash(start: datetime, end: datetime, reason: str, note: str) -> str:
    raw = f"{start.isoformat()}|{end.isoformat()}|{reason}|{note}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _get_locked_settled_shift(db: Session, shift_id: int) -> Shift:
    """取出班次行（FOR UPDATE 串行化该班次的所有写操作）并要求已结算。"""
    shift = db.execute(
        select(Shift).where(Shift.id == shift_id).with_for_update()
    ).scalar_one_or_none()
    if shift is None:
        raise ApiError(404, "SHIFT_NOT_FOUND", f"unknown shift id={shift_id}")
    if _latest_revision(db, shift_id) is None:
        raise ApiError(
            409,
            "SHIFT_NOT_SETTLED",
            f"shift id={shift_id} has not been settled; "
            "manual adjudication is only allowed after settlement",
        )
    return shift


def _recompute_after_ruling(
    db: Session, shift: Shift, trigger: str
) -> ShiftResult | None:
    """裁定/撤销后重算班次：仅当各原因分钟数变化时追加新修订并记录差额。"""
    latest = _latest_revision(db, shift.id)
    totals, used = _compute(db, shift)
    if (
        totals["maintenance"] == latest.maintenance_minutes
        and totals["failure"] == latest.failure_minutes
        and totals["pending"] == latest.pending_minutes
    ):
        return None  # 分钟数无变化：无修订、无变更，历史修订保持不变
    rev = _new_revision(db, shift, totals, used, trigger=trigger, base=latest)
    db.flush()
    return rev


def submit_adjudication(
    db: Session, shift_id: int, payload: AdjudicationIn
) -> tuple[Shift, ManualAdjudication, bool, ShiftResult | None, int]:
    """为已结算班次提交现场人工裁定。

    返回 (班次, 裁定, 是否幂等命中, 新修订或 None, 当前最新修订号)。
    - 同单号同内容重试：幂等返回，不产生修订；
    - 同单号异内容（含已撤销单号复用）：409 RULING_CONFLICT；
    - 区间越出班次 / 班次未结算 / 未知班次：分别明确报错。
    """
    shift = _get_locked_settled_shift(db, shift_id)
    start, end = _utc(payload.start), _utc(payload.end)
    if start < shift.start_ts or end > shift.end_ts:
        raise ApiError(
            422,
            "RULING_OUT_OF_RANGE",
            f"ruling [{start.isoformat()}, {end.isoformat()}) is not within "
            f"shift [{shift.start_ts.isoformat()}, {shift.end_ts.isoformat()})",
        )
    note = payload.note or ""
    digest = _ruling_hash(start, end, payload.reason, note)

    existing = db.execute(
        select(ManualAdjudication).where(
            ManualAdjudication.shift_id == shift_id,
            ManualAdjudication.ruling_no == payload.ruling_no,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.content_hash == digest:
            db.commit()  # 同单号同内容（含已撤销后再提交同内容）：幂等
            return shift, existing, True, None, _latest_revision(db, shift_id).revision
        raise ApiError(
            409,
            "RULING_CONFLICT",
            f"ruling_no {payload.ruling_no!r} already exists on shift "
            f"{shift_id} with different content",
        )

    ruling = ManualAdjudication(
        shift_id=shift_id,
        ruling_no=payload.ruling_no,
        start_ts=start,
        end_ts=end,
        reason=payload.reason,
        note=note,
        content_hash=digest,
        status="active",
    )
    db.add(ruling)
    try:
        db.flush()  # 班次行锁内插入，id 顺序即裁定生效顺序；唯一约束兜底并发竞争
    except IntegrityError:
        db.rollback()
        # 并发同单号提交：重新判定幂等 / 冲突
        shift2 = _get_locked_settled_shift(db, shift_id)
        other = db.execute(
            select(ManualAdjudication).where(
                ManualAdjudication.shift_id == shift_id,
                ManualAdjudication.ruling_no == payload.ruling_no,
            )
        ).scalar_one()
        if other.content_hash != digest:
            raise ApiError(
                409,
                "RULING_CONFLICT",
                f"ruling_no {payload.ruling_no!r} already exists on shift "
                f"{shift_id} with different content",
            )
        return (
            shift2,
            other,
            True,
            None,
            _latest_revision(db, shift_id).revision,
        )

    rev = _recompute_after_ruling(db, shift, trigger="adjudication")
    db.commit()
    db.refresh(ruling)
    latest_rev = _latest_revision(db, shift_id)
    return shift, ruling, False, rev, latest_rev.revision


def revoke_adjudication(
    db: Session, shift_id: int, ruling_no: str
) -> tuple[Shift, ManualAdjudication, bool, ShiftResult | None, int]:
    """按单号撤销班次的现场人工裁定。

    返回 (班次, 裁定, 是否幂等命中, 新修订或 None, 当前最新修订号)。
    - 已撤销再撤销：幂等返回，不产生修订；
    - 未知班次 / 未知单号：分别 404；班次未结算不允许出现裁定，同样明确拒绝。
    撤销生效后重叠分钟回退到此前仍有效的裁定；无裁定时按当前最新事件版本计算。
    """
    shift = _get_locked_settled_shift(db, shift_id)
    ruling = db.execute(
        select(ManualAdjudication).where(
            ManualAdjudication.shift_id == shift_id,
            ManualAdjudication.ruling_no == ruling_no,
        )
    ).scalar_one_or_none()
    if ruling is None:
        raise ApiError(
            404,
            "RULING_NOT_FOUND",
            f"unknown ruling_no {ruling_no!r} on shift {shift_id}",
        )
    if ruling.status == "revoked":
        db.commit()  # 重复撤销幂等
        return shift, ruling, True, None, _latest_revision(db, shift_id).revision

    ruling.status = "revoked"
    ruling.revoked_at = datetime.now(timezone.utc)
    db.flush()
    rev = _recompute_after_ruling(db, shift, trigger="adjudication_revoke")
    db.commit()
    db.refresh(ruling)
    latest_rev = _latest_revision(db, shift_id)
    return shift, ruling, False, rev, latest_rev.revision


def list_adjudications(db: Session, shift_id: int) -> tuple[Shift, list]:
    """列出班次的全部现场人工裁定（含已撤销），按生效顺序排列。"""
    shift = get_shift_or_404(db, shift_id)
    rows = (
        db.execute(
            select(ManualAdjudication)
            .where(ManualAdjudication.shift_id == shift_id)
            .order_by(ManualAdjudication.id)
        )
        .scalars()
        .all()
    )
    return shift, list(rows)


# ---------------------------------------------------------------- 结果查询


def get_result(
    db: Session, shift_id: int, revision: int | None
) -> tuple[Shift, ShiftResult, int]:
    """返回 (班次, 指定/最新修订, 最新修订号)。"""
    shift = get_shift_or_404(db, shift_id)
    latest = _latest_revision(db, shift_id)
    if latest is None:
        raise ApiError(
            404, "RESULT_NOT_FOUND", f"shift id={shift_id} has not been settled"
        )
    if revision is None:
        return shift, latest, latest.revision
    rev = db.execute(
        select(ShiftResult).where(
            ShiftResult.shift_id == shift_id, ShiftResult.revision == revision
        )
    ).scalar_one_or_none()
    if rev is None:
        raise ApiError(
            404, "REVISION_NOT_FOUND", f"shift id={shift_id} has no revision {revision}"
        )
    return shift, rev, latest.revision


def list_revisions(db: Session, shift_id: int) -> tuple[Shift, list[ShiftResult]]:
    shift = get_shift_or_404(db, shift_id)
    revs = (
        db.execute(
            select(ShiftResult)
            .where(ShiftResult.shift_id == shift_id)
            .order_by(ShiftResult.revision)
        )
        .scalars()
        .all()
    )
    return shift, list(revs)


# ---------------------------------------------------------------- 变更同步

#: GET /changes 允许的页大小上限
MAX_PAGE_SIZE = 500


def list_changes(
    db: Session, cursor: int, limit: int
) -> tuple[list[tuple[ShiftResult, Shift]], int]:
    """按全局递增游标分页读取结算/更正变更，返回 ([(修订, 班次)], 下一游标)。

    - 返回 seq > cursor 的变更，按 seq 升序，最多 limit 条；
    - 空页保持原游标（客户端已追平，可原地轮询）；
    - 游标超过当前最大序号视为非法（客户端进度与服务端不一致），
      明确拒绝且不影响任何同步进度。
    """
    if cursor > 0:
        max_seq = db.execute(select(func.max(ShiftResult.seq))).scalar() or 0
        if cursor > max_seq:
            raise ApiError(
                422,
                "INVALID_CURSOR",
                f"cursor {cursor} is beyond the latest change seq {max_seq}; "
                "restart sync from a smaller cursor (0 replays from the beginning)",
            )
    rows = db.execute(
        select(ShiftResult, Shift)
        .join(Shift, Shift.id == ShiftResult.shift_id)
        .where(ShiftResult.seq > cursor)
        .order_by(ShiftResult.seq)
        .limit(limit)
    ).all()
    page = [(rev, shift) for rev, shift in rows]
    next_cursor = page[-1][0].seq if page else cursor
    return page, next_cursor
