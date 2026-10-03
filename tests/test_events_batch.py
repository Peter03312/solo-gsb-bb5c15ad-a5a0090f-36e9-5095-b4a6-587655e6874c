"""现场网关断网批量补传（POST /events/batch）端到端测试。

需要可用的 PostgreSQL（在 Compose 环境内运行）：
docker compose exec app pytest tests/test_events_batch.py
"""

import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import engine
from app.main import app

TABLES = (
    "manual_adjudications",
    "shift_results",
    "event_versions",
    "event_sources",
    "shifts",
    "devices",
    "change_counter",
)

DEVICE = {"id": "CNC-01", "name": "加工中心1号"}
SHIFT = {
    "device_id": "CNC-01",
    "start": "2026-10-01T08:00:00+08:00",
    "end": "2026-10-01T16:00:00+08:00",
}


def _ev(source, version, s, e, reason="failure"):
    return {
        "device_id": "CNC-01",
        "source_id": source,
        "version": version,
        "start": f"2026-10-01T{s}",
        "end": f"2026-10-01T{e}",
        "reason": reason,
    }


@pytest.fixture()
def client():
    with TestClient(app) as c:  # 触发 lifespan：建库建表与迁移
        with engine.begin() as conn:
            for t in TABLES:
                conn.execute(text(f"TRUNCATE {t} CASCADE"))
        yield c


def _make_settled_shift(client, events):
    assert client.post("/devices", json=DEVICE).status_code == 201
    r = client.post("/shifts", json=SHIFT)
    assert r.status_code == 201
    shift_id = r.json()["id"]
    for ev in events:
        assert client.post("/events", json=ev).status_code == 201
    assert client.post(f"/shifts/{shift_id}/settle").status_code == 200
    return shift_id


def _batch(client, events, device_id="CNC-01"):
    return client.post(
        "/events/batch", json={"device_id": device_id, "events": events}
    )


# ---------------------------------------------------------------- 基础成功 / 幂等


def test_batch_creates_multiple_sources_and_marks_items(client):
    shift_id = _make_settled_shift(client, [_ev("MES-1", 1, "09:00:00+08:00", "10:00:00+08:00", "maintenance")])
    r = _batch(client, [
        _ev("PLC-9", 1, "10:00:00+08:00", "11:00:00+08:00", "failure"),
        _ev("OP-1", 1, "11:00:00+08:00", "11:30:00+08:00", "pending"),
    ])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["device_id"] == "CNC-01"
    assert body["status"] == "applied"
    assert [(i["index"], i["source_id"], i["version"], i["status"]) for i in body["items"]] == [
        (0, "PLC-9", 1, "created"),
        (1, "OP-1", 1, "created"),
    ]
    assert all(i["idempotent"] is False for i in body["items"])
    # 两个来源都落在班次窗口内，只产生一次修订
    assert len(body["recomputed"]) == 1
    assert body["recomputed"][0]["shift_id"] == shift_id
    assert body["recomputed"][0]["revision"] == 2


def test_batch_all_idempotent_returns_200(client):
    ev = _ev("PLC-9", 1, "10:00:00+08:00", "11:00:00+08:00")
    _make_settled_shift(client, [ev])
    r = _batch(client, [ev, ev])  # 列表内重复：同版本同内容均可重试
    assert r.status_code == 200, r.text
    body = r.json()
    assert [i["status"] for i in body["items"]] == ["idempotent", "idempotent"]
    assert body["recomputed"] == []


def test_batch_mixed_idempotent_and_created_returns_201(client):
    ev = _ev("PLC-9", 1, "10:00:00+08:00", "11:00:00+08:00")
    _make_settled_shift(client, [ev])
    r = _batch(client, [
        ev,
        _ev("PLC-9", 2, "10:00:00+08:00", "10:30:00+08:00"),
    ])
    assert r.status_code == 201
    assert [i["status"] for i in r.json()["items"]] == ["idempotent", "created"]


def test_batch_interleaved_sources_checked_in_list_order(client):
    _make_settled_shift(client, [_ev("A", 1, "09:00:00+08:00", "09:30:00+08:00")])
    r = _batch(client, [
        _ev("A", 2, "09:00:00+08:00", "09:40:00+08:00"),
        _ev("B", 1, "13:00:00+08:00", "13:30:00+08:00"),
        _ev("A", 3, "09:00:00+08:00", "09:50:00+08:00"),
    ])
    assert r.status_code == 201, r.text
    assert [i["status"] for i in r.json()["items"]] == [
        "created", "created", "created"
    ]


def test_batch_out_of_order_idempotent_items_use_final_version(client):
    """列表中既有旧版本幂等重试又有新版本时，重算依据的始终是最终最新版本。"""
    shift_id = _make_settled_shift(client, [
        _ev("A", 1, "10:00:00+08:00", "12:00:00+08:00", "maintenance"),
    ])
    # 乱序：先幂等 v1，再新建 v2，再幂等 v1 —— 最终生效仍为 v2
    r = _batch(client, [
        _ev("A", 1, "10:00:00+08:00", "12:00:00+08:00", "maintenance"),
        _ev("A", 2, "10:00:00+08:00", "11:00:00+08:00", "maintenance"),
        _ev("A", 1, "10:00:00+08:00", "12:00:00+08:00", "maintenance"),
    ])
    assert r.status_code == 201, r.text
    assert [i["status"] for i in r.json()["items"]] == [
        "idempotent", "created", "idempotent"
    ]
    rec = r.json()["recomputed"]
    assert len(rec) == 1 and rec[0]["shift_id"] == shift_id
    assert rec[0]["totals"]["maintenance"] == 60
    versions = {u["source_id"]: u["version"]
                for u in client.get(f"/shifts/{shift_id}/result").json()["event_versions"]}
    assert versions == {"A": 2}


# ---------------------------------------------------------------- 整批失败


def test_batch_empty_list_rejected(client):
    assert client.post("/devices", json=DEVICE).status_code == 201
    r = _batch(client, [])
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "VALIDATION_ERROR"


def test_batch_invalid_item_field_rejected(client):
    assert client.post("/devices", json=DEVICE).status_code == 201
    # 末项原因非法、区间非法、秒未对齐 —— 整批 422
    bad_lists = [
        [{**_ev("A", 1, "10:00:00+08:00", "11:00:00+08:00"), "reason": "unknown"}],
        [{**_ev("A", 1, "11:00:00+08:00", "10:00:00+08:00")}],
        [{**_ev("A", 1, "10:00:30+08:00", "11:00:00+08:00")}],
        [{**_ev("A", 1, "10:00:00", "11:00:00")}],  # 缺时区
        [{**_ev("", 1, "10:00:00+08:00", "11:00:00+08:00")}],
        [{**_ev("A", 0, "10:00:00+08:00", "11:00:00+08:00")}],
    ]
    for events in bad_lists:
        r = _batch(client, events)
        assert r.status_code == 422, events
        assert r.json()["detail"]["code"] == "VALIDATION_ERROR"


def test_batch_unknown_device_rejected(client):
    r = _batch(client, [_ev("A", 1, "10:00:00+08:00", "11:00:00+08:00")], device_id="NOPE")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "DEVICE_NOT_FOUND"


def test_batch_version_conflict_aborts_whole_batch(client):
    shift_id = _make_settled_shift(client, [
        _ev("A", 1, "09:00:00+08:00", "10:00:00+08:00", "maintenance"),
    ])
    # 首项可建、第二项与既有版本 1 异内容 → 整批失败
    r = _batch(client, [
        _ev("B", 1, "11:00:00+08:00", "11:30:00+08:00"),
        {**_ev("A", 1, "09:00:00+08:00", "10:30:00+08:00", "maintenance")},
    ])
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "VERSION_CONFLICT"
    _assert_no_trace_left(client, shift_id)


def test_batch_version_gap_aborts_whole_batch(client):
    shift_id = _make_settled_shift(client, [
        _ev("A", 1, "09:00:00+08:00", "10:00:00+08:00", "maintenance"),
    ])
    # 新来源首版本跳号；批内来源 A 先正确推进再出现 B 跳号 → 整批失败
    r = _batch(client, [
        _ev("A", 2, "09:00:00+08:00", "10:30:00+08:00", "maintenance"),
        _ev("B", 3, "11:00:00+08:00", "11:30:00+08:00"),
    ])
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "VERSION_GAP"
    _assert_no_trace_left(client, shift_id)


def test_batch_intra_list_gap_aborts(client):
    _make_settled_shift(client, [])
    # 同来源批内跳号：缺版本 2
    r = _batch(client, [
        _ev("A", 1, "09:00:00+08:00", "09:30:00+08:00"),
        _ev("A", 3, "09:00:00+08:00", "09:40:00+08:00"),
    ])
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "VERSION_GAP"


def test_batch_same_version_twice_with_different_content_aborts(client):
    """批内重复同一新版本号：同内容第二次幂等；异内容按 VERSION_CONFLICT 整批失败。"""
    _make_settled_shift(client, [])
    v1 = _ev("A", 1, "09:00:00+08:00", "09:30:00+08:00")
    r = _batch(client, [v1, v1])  # 同内容重复：第二项幂等
    assert r.status_code == 201
    assert [i["status"] for i in r.json()["items"]] == ["created", "idempotent"]
    # 异内容重复：整批 VERSION_CONFLICT（该来源此前无任何版本，首项尚未提交）
    r = _batch(client, [
        _ev("B", 1, "09:00:00+08:00", "09:30:00+08:00"),
        _ev("B", 1, "09:00:00+08:00", "09:40:00+08:00"),
    ])
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "VERSION_CONFLICT"
    with engine.connect() as conn:
        assert conn.execute(text(
            "SELECT count(*) FROM event_versions WHERE source_id='B'"
        )).scalar() == 0


def _assert_no_trace_left(client, shift_id):
    # 无新事件、无来源游标推进、无修订/变更残留
    r = client.get(f"/shifts/{shift_id}/revisions")
    assert [x["revision"] for x in r.json()] == [1]
    with engine.connect() as conn:
        # 只有结算时既有的 A v1
        rows = conn.execute(text(
            "SELECT source_id, version FROM event_versions ORDER BY source_id, version"
        )).all()
        assert rows == [("A", 1)]
        rows = conn.execute(text(
            "SELECT source_id, last_version FROM event_sources ORDER BY source_id"
        )).all()
        assert rows == [("A", 1)]
        assert conn.execute(text("SELECT max(seq) FROM shift_results")).scalar() == 1


# ---------------------------------------------------------------- 并集窗口与最终状态重算


def test_batch_recomputes_once_per_shift_from_final_state(client):
    # 班次内：A v1=保养 10:00-12:00；结算 maintenance=120
    shift_id = _make_settled_shift(client, [
        _ev("A", 1, "10:00:00+08:00", "12:00:00+08:00", "maintenance"),
    ])
    # 一批内 A 连续推到 v3：最终为保养 10:00-11:00（maintenance 60）；
    # 另一来源 B v1 故障 11:00-12:00（failure 60）
    r = _batch(client, [
        _ev("A", 2, "10:00:00+08:00", "12:00:00+08:00", "pending"),
        _ev("B", 1, "11:00:00+08:00", "12:00:00+08:00", "failure"),
        _ev("A", 3, "10:00:00+08:00", "11:00:00+08:00", "maintenance"),
    ])
    assert r.status_code == 201, r.text
    rec = r.json()["recomputed"]
    # 每个班次最多一次修订
    assert len(rec) == 1 and rec[0]["shift_id"] == shift_id
    assert rec[0]["revision"] == 2
    assert rec[0]["totals"] == {"maintenance": 60, "failure": 60, "pending": 0}
    assert rec[0]["delta"] == {"maintenance": -60, "failure": 60, "pending": 0}
    # 修订依据的是最终版本
    body = client.get(f"/shifts/{shift_id}/result").json()
    assert {u["source_id"]: u["version"] for u in body["event_versions"]} == {
        "A": 3, "B": 1
    }


def test_batch_old_interval_union_covers_shifts(client):
    """批后最新版本区间不再覆盖、但旧版本区间覆盖的班次仍须重算。"""
    assert client.post("/devices", json=DEVICE).status_code == 201
    # 早班 / 晚班
    s1 = client.post("/shifts", json={
        "device_id": "CNC-01",
        "start": "2026-10-01T08:00:00+08:00",
        "end": "2026-10-01T16:00:00+08:00",
    }).json()["id"]
    s2 = client.post("/shifts", json={
        "device_id": "CNC-01",
        "start": "2026-10-01T16:00:00+08:00",
        "end": "2026-10-02T00:00:00+08:00",
    }).json()["id"]
    # 故障 10:00-18:00 横跨两班，结算
    ev = _ev("A", 1, "10:00:00+08:00", "18:00:00+08:00")
    assert client.post("/events", json=ev).status_code == 201
    client.post(f"/shifts/{s1}/settle")
    client.post(f"/shifts/{s2}/settle")
    # 批量把 A 推到 v2：缩短为 10:00-12:00（仅覆盖早班）→ 晚班因旧区间并集仍被重算
    r = _batch(client, [_ev("A", 2, "10:00:00+08:00", "12:00:00+08:00")])
    assert r.status_code == 201
    rec = {x["shift_id"]: x for x in r.json()["recomputed"]}
    assert set(rec) == {s1, s2}
    assert rec[s1]["totals"]["failure"] == 120
    assert rec[s1]["delta"]["failure"] == -240
    assert rec[s2]["totals"]["failure"] == 0
    assert rec[s2]["delta"]["failure"] == -120
    # 每班次仅一次修订
    assert client.get(f"/shifts/{s1}/revisions").json()[-1]["revision"] == 2
    assert client.get(f"/shifts/{s2}/revisions").json()[-1]["revision"] == 2


def test_batch_no_minute_change_no_revision(client):
    shift_id = _make_settled_shift(client, [
        _ev("A", 1, "10:00:00+08:00", "11:00:00+08:00"),
    ])
    # 新版本同内容（分钟数不变）
    r = _batch(client, [_ev("A", 2, "10:00:00+08:00", "11:00:00+08:00")])
    assert r.status_code == 201
    assert r.json()["recomputed"] == []
    assert [x["revision"] for x in client.get(f"/shifts/{shift_id}/revisions").json()] == [1]


def test_batch_change_feed_one_entry_per_shift(client):
    shift_id = _make_settled_shift(client, [
        _ev("A", 1, "10:00:00+08:00", "12:00:00+08:00", "maintenance"),
    ])
    before_max = client.get("/changes", params={"cursor": 0, "limit": 500}).json()["next_cursor"]
    assert before_max == 1
    r = _batch(client, [
        _ev("A", 2, "10:00:00+08:00", "11:00:00+08:00", "maintenance"),
        _ev("A", 3, "10:00:00+08:00", "10:30:00+08:00", "maintenance"),
    ])
    assert r.status_code == 201
    page = client.get("/changes", params={"cursor": before_max, "limit": 500}).json()
    assert len(page["changes"]) == 1  # 每班次一条变更
    ch = page["changes"][0]
    assert ch["shift_id"] == shift_id and ch["revision"] == 2
    assert ch["trigger"] == "correction"
    assert ch["totals"] == {"maintenance": 30, "failure": 0, "pending": 0}
    assert page["next_cursor"] == before_max + 1


# ---------------------------------------------------------------- 与单条上报兼容 / 并发


def test_batch_recompute_respects_active_ruling(client):
    """批次重算遵守既有现场裁定：被裁定覆盖的分钟不被事件改动。"""
    shift_id = _make_settled_shift(client, [
        _ev("A", 1, "10:00:00+08:00", "12:00:00+08:00", "maintenance"),
    ])
    # 结算：maintenance=120。裁定 10:00-11:00 为待判
    r = client.post(f"/shifts/{shift_id}/adjudications", json={
        "ruling_no": "R-1",
        "start": "2026-10-01T10:00:00+08:00",
        "end": "2026-10-01T11:00:00+08:00",
        "reason": "pending",
        "note": "现场改判",
    })
    assert r.status_code == 201
    # 批次把 A v2 缩为 10:00-10:30（保养）：裁定覆盖的 10:00-11:00 不受影响，
    # 只有 11:00-12:00 的保养 60 分钟被移除
    r = _batch(client, [_ev("A", 2, "10:00:00+08:00", "10:30:00+08:00", "maintenance")])
    assert r.status_code == 201, r.text
    rec = r.json()["recomputed"]
    assert len(rec) == 1
    # 修订前：maintenance=60（11-12），pending=60（10-11 裁定）
    # 修订后：10:00-10:30 的保养被裁掉，但该半小时本就是 pending 裁定 → 不变；
    # 其余分钟无事件：maintenance=0, pending=60
    assert rec[0]["totals"] == {"maintenance": 0, "failure": 0, "pending": 60}
    assert rec[0]["delta"] == {"maintenance": -60, "failure": 0, "pending": 0}
    body = client.get(f"/shifts/{shift_id}/result").json()
    assert body["revision"] == rec[0]["revision"]


def test_batch_then_single_continues_chain(client):
    _make_settled_shift(client, [])
    r = _batch(client, [
        _ev("A", 1, "09:00:00+08:00", "09:30:00+08:00"),
        _ev("A", 2, "09:00:00+08:00", "09:40:00+08:00"),
    ])
    assert r.status_code == 201
    # 单条接口接链：v3 成功，v4 跳号失败
    assert client.post("/events", json=_ev("A", 3, "09:00:00+08:00", "09:50:00+08:00")).status_code == 201
    r = client.post("/events", json=_ev("A", 5, "09:00:00+08:00", "09:50:00+08:00"))
    assert r.status_code == 409 and r.json()["detail"]["code"] == "VERSION_GAP"
    # 单条幂等与冲突语义不变
    assert client.post("/events", json=_ev("A", 2, "09:00:00+08:00", "09:40:00+08:00")).json()["idempotent"] is True
    r = client.post("/events", json=_ev("A", 2, "09:00:00+08:00", "09:45:00+08:00"))
    assert r.status_code == 409 and r.json()["detail"]["code"] == "VERSION_CONFLICT"


def test_concurrent_batch_and_single_keep_chain_contiguous(client):
    """并发：一个批次与多个单条上报并发推进同一来源，版本链始终连续。"""
    _make_settled_shift(client, [])
    n_single = 5
    n_batch = 5  # 批次提交版本 6..10
    barrier = threading.Barrier(n_single + 1)
    results: list[tuple[str, int]] = []
    lock = threading.Lock()

    def single(ver: int):
        try:
            barrier.wait(timeout=15)
            r = client.post("/events", json=_ev(
                "S", ver, "09:00:00+08:00", f"09:{ver:02d}:00+08:00"
            ))
            with lock:
                results.append(("single", r.status_code))
        except Exception as exc:  # noqa: BLE001
            with lock:
                results.append(("single-err", repr(exc)))

    def batch():
        try:
            barrier.wait(timeout=15)
            r = _batch(client, [
                _ev("S", 6 + i, "10:00:00+08:00", f"10:{5 + i:02d}:00+08:00")
                for i in range(n_batch)
            ])
            with lock:
                results.append(("batch", r.status_code))
        except Exception as exc:  # noqa: BLE001
            with lock:
                results.append(("batch-err", repr(exc)))

    threads = [threading.Thread(target=single, args=(v,)) for v in range(1, n_single + 1)]
    threads.append(threading.Thread(target=batch))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 所有单条最终都成功（可能被批次先行后转为冲突——此时重试即可），
    # 批次要么整体成功（201）要么因单条先行导致跳号 409；两种结局下链都连续。
    # 简化判定：重试单条到全部成功，最终 last_version 必须为 10 且版本 1..10 齐全。
    for ver in range(1, n_single + 1):
        for _ in range(12):
            r = client.post("/events", json=_ev(
                "S", ver, "09:00:00+08:00", f"09:{ver:02d}:00+08:00"
            ))
            if r.status_code in (200, 201):
                break
            assert r.status_code == 409
            code = r.json()["detail"]["code"]
            assert code in ("VERSION_GAP", "VERSION_CONFLICT")
            if code == "VERSION_CONFLICT":
                break
    # 并发竞争中批次可能因单条尚未全部到达而跳号（409，整批不留痕）；
    # 竞争结束后重试必须整体成功。
    batch_payload = [
        _ev("S", 6 + i, "10:00:00+08:00", f"10:{5 + i:02d}:00+08:00")
        for i in range(n_batch)
    ]
    for _ in range(12):
        r = _batch(client, batch_payload)
        if r.status_code in (200, 201):
            break
        assert r.status_code == 409
        assert r.json()["detail"]["code"] in ("VERSION_GAP", "VERSION_CONFLICT")
    assert r.status_code in (200, 201)

    with engine.connect() as conn:
        last = conn.execute(text(
            "SELECT last_version FROM event_sources WHERE source_id='S'"
        )).scalar()
        versions = [r[0] for r in conn.execute(text(
            "SELECT version FROM event_versions WHERE source_id='S' ORDER BY version"
        )).all()]
    assert last == 10
    assert versions == list(range(1, 11))
