# 设备停机归因服务（Downtime Attribution）

按**班次**对设备停机时间进行归因的 HTTP 服务：输入设备、班次（带时区、整分钟）与停机事件
（来源编号 + 连续递增版本 + 起止 + 原因），输出各班次的**保养 / 故障 / 待判**分钟数及所依据
的事件版本；支持事件幂等上报、版本链校验、迟到更正重算与结算修订查询；已结算班次还可登记
**现场人工裁定**（唯一单号、整分钟区间、原因/说明，可按单号撤销），裁定按生效顺序覆盖自动
事件归因、撤销后回退；所有结果变化通过**全局递增游标**的变更流（`GET /changes`）供下游报表
持续同步每次结算、更正、裁定与撤销。

技术栈：Python 3.12 · FastAPI · SQLAlchemy 2 · PostgreSQL 16 · Docker Compose

## 快速启动

```bash
docker compose up --build
```

一条命令完成依赖安装、PostgreSQL 建库建表（含扩展与约束）与服务启动。

| 地址 | 说明 |
| --- | --- |
| `http://localhost:8000` | API 根地址 |
| `http://localhost:8000/docs` | Swagger 交互文档（可在线试调用） |
| `http://localhost:8000/health` | 健康检查 |
| `localhost:5432` | PostgreSQL（用户/密码 `postgres`，库 `downtime`） |

冒烟验证（覆盖下文全部流程）：

```bash
./scripts/smoke.sh                 # 或指定地址 ./scripts/smoke.sh http://localhost:8000
```

运行测试：

```bash
docker compose exec app pytest            # 算法单测 + API 端到端测试
```

## 配置

通过环境变量配置（`docker-compose.yml` 已为 `app` 服务注入）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg://postgres:postgres@localhost:5432/downtime` | SQLAlchemy 连接串 |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `postgres` / `postgres` / `downtime` | Compose 中数据库初始化参数 |

端口映射：`8000`（API）、`5432`（数据库），可在 `docker-compose.yml` 中调整。

## 业务规则

- **半开区间**：班次与事件均为 `[start, end)`；端点相接不算重叠、不漏分钟。
- **时间戳**：必须带时区且精确到整分钟（秒须为 `00`），否则 `422`。
- **班次**：同一设备的班次区间不可重叠（应用校验 + 数据库排他约束双保险），冲突 `409`。
- **事件版本链**：每个 `(设备, 来源)` 的版本从 1 开始连续递增。
  - 同来源 + 同版本 + 同内容 → 幂等返回（`200`，`idempotent=true`），不产生新数据；
  - 同版本异内容 → `409 VERSION_CONFLICT`；跳号 → `409 VERSION_GAP`；
  - 新版本即对旧版本的更正，当前生效事件 = 每个来源的最新版本。
- **归因**：窗口内每分钟只归因一次，重叠时 **保养 > 故障 > 待判**；结果含各原因分钟数
  与实际贡献分钟的事件版本列表。
- **现场人工裁定**（仅对已结算班次）：
  - 提交内容为 `单号 + 班次内整分钟半开区间 + 原因（保养/故障/待判）+ 说明`；
    区间必须落在班次 `[start, end)` 内（可与班次端点对齐），否则 `422 RULING_OUT_OF_RANGE`；
  - 裁定分钟**绝对覆盖**自动事件归因（含保养事件）；重叠裁定之间不比原因优先级，
    每分钟以**生效顺序最新的未撤销裁定**为准（生效顺序即落库先后），撤销后自然
    回退到此前仍有效的裁定，全部撤销后回到事件归因（按当前最新事件版本计算）；
  - 同单号同内容重试幂等（`200`），同单号异内容 `409 RULING_CONFLICT`；
    重复撤销幂等，未知班次 `404 SHIFT_NOT_FOUND`、未知单号 `404 RULING_NOT_FOUND`、
    班次未结算 `409 SHIFT_NOT_SETTLED`；
  - 裁定提交、撤销及裁定生效期间的事件更正，**仅在各原因分钟数变化时**产生班次修订
    （无修订时回执中 `revision=null` 明确标示）与 /changes 变更，历史修订永不重写。
- **结算与修订**：`POST /shifts/{id}/settle` 结算生成修订 1 并永久保留；迟到更正按事件时间
  （新旧版本区间的并集）重算受影响的已结算班次，现场裁定按上述规则一并参与重算；结果变化时
  追加新修订并记录与上一修订的差额（`delta`）；查询可用 `?revision=N` 指定历史修订。
- **变更同步**：每次结算以及每次导致结果变化的更正 / 裁定 / 撤销产生一条变更记录
 （`seq` 全局递增，`trigger` 为 `settle` / `correction` / `adjudication` /
  `adjudication_revoke`），`GET /changes?cursor=&limit=` 按游标分页读取；修订与变更记录
  同一行存储、同事务提交/回滚，序号顺序即提交顺序，已返回条目不可变；空页保持原游标，
  非法游标/超限页大小返回 `422`。
- **错误**：未知设备 `404 DEVICE_NOT_FOUND`，非法区间 `422`，未结算 `404 RESULT_NOT_FOUND`，
  未知班次/修订 `404`。错误体统一为 `{"detail": {"code": "...", "message": "..."}}`。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/devices` | 注册设备（同 id 重复提交幂等返回 200） |
| `GET` | `/devices` | 设备列表 |
| `POST` | `/shifts` | 创建班次（重叠 409 / 未知设备 404 / 非法区间 422） |
| `GET` | `/shifts/{id}` | 班次详情 |
| `POST` | `/events` | 上报事件版本（新建 201 / 幂等 200 / 冲突·跳号 409） |
| `POST` | `/shifts/{id}/settle` | 结算班次（重复调用幂等返回最新修订） |
| `GET` | `/shifts/{id}/result?revision=N` | 查询归因结果，缺省最新修订 |
| `GET` | `/shifts/{id}/revisions` | 全部修订及差额 |
| `POST` | `/shifts/{id}/adjudications` | 提交现场人工裁定（新建 201 / 幂等 200 / 单号冲突 409 / 未结算 409 / 越界 422） |
| `POST` | `/shifts/{id}/adjudications/{rulingNo}/revoke` | 按单号撤销裁定（首次 201 / 重复撤销 200 / 未知单号 404） |
| `GET` | `/shifts/{id}/adjudications` | 班次的全部裁定（含已撤销，按生效顺序） |
| `GET` | `/changes?cursor=&limit=` | 变更同步：按全局递增游标分页读取结算/更正/裁定/撤销变更 |
| `GET` | `/health` | 健康检查 |

原因枚举：`maintenance`（保养）、`failure`（故障）、`pending`（待判）。

错误体统一为 `{"detail": {"code": "...", "message": "..."}}`：

| HTTP | code | 含义 |
| --- | --- | --- |
| 404 | `DEVICE_NOT_FOUND` | 未知设备 |
| 404 | `SHIFT_NOT_FOUND` | 未知班次 |
| 404 | `RESULT_NOT_FOUND` | 班次尚未结算 |
| 404 | `REVISION_NOT_FOUND` | 指定修订不存在 |
| 409 | `SHIFT_OVERLAP` | 同设备班次区间重叠 |
| 409 | `VERSION_CONFLICT` | 同来源同版本但内容不同 |
| 409 | `VERSION_GAP` | 版本跳号（含首版本不为 1） |
| 409 | `SHIFT_NOT_SETTLED` | 班次尚未结算，不能登记现场裁定 |
| 409 | `RULING_CONFLICT` | 裁定单号已存在但内容不同（含复用已撤销单号） |
| 404 | `RULING_NOT_FOUND` | 班次下未知裁定单号 |
| 422 | `RULING_OUT_OF_RANGE` | 裁定区间越出班次窗口 |
| 422 | `VALIDATION_ERROR` | 非法参数：区间无效、缺时区、未对齐整分钟、原因非法、游标/页大小越界等 |
| 422 | `INVALID_CURSOR` | 同步游标超过当前最大变更序号（客户端进度异常，需从较小游标重新同步） |

## 调用示例

以下完整走一遍主流程（假设服务运行于 `http://localhost:8000`）。

**1. 注册设备**

```bash
curl -X POST http://localhost:8000/devices \
  -H 'Content-Type: application/json' \
  -d '{"id": "CNC-01", "name": "加工中心1号"}'
# 201 {"id":"CNC-01","name":"加工中心1号"}
```

**2. 创建班次**（早班 08:00–16:00，+08:00）

```bash
curl -X POST http://localhost:8000/shifts \
  -H 'Content-Type: application/json' \
  -d '{"device_id":"CNC-01","start":"2026-10-01T08:00:00+08:00","end":"2026-10-01T16:00:00+08:00"}'
# 201 {"id":1,"device_id":"CNC-01","start":"2026-10-01T00:00:00Z","end":"2026-10-01T08:00:00Z"}
```

重叠班次返回 `409 {"detail":{"code":"SHIFT_OVERLAP", ...}}`；端点相接（如 16:00 起）允许。

**3. 上报停机事件**：保养 10:00–11:00

```bash
curl -X POST http://localhost:8000/events \
  -H 'Content-Type: application/json' \
  -d '{"device_id":"CNC-01","source_id":"MES-1","version":1,
       "start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:00:00+08:00",
       "reason":"maintenance"}'
# 201 {"device_id":"CNC-01","source_id":"MES-1","version":1,"idempotent":false,"recomputed":[]}
```

**4. 幂等与版本校验**

```bash
# 原样重发 → 200，idempotent=true
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"device_id":"CNC-01","source_id":"MES-1","version":1,
       "start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:00:00+08:00","reason":"maintenance"}'

# 同版本异内容 → 409 VERSION_CONFLICT
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"device_id":"CNC-01","source_id":"MES-1","version":1,
       "start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:30:00+08:00","reason":"maintenance"}'

# 跳号（应为 2 却发 3）→ 409 VERSION_GAP
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"device_id":"CNC-01","source_id":"MES-1","version":3,
       "start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:00:00+08:00","reason":"maintenance"}'
```

**5. 再上报一条故障** 10:30–12:00（另一来源）

```bash
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"device_id":"CNC-01","source_id":"PLC-9","version":1,
       "start":"2026-10-01T10:30:00+08:00","end":"2026-10-01T12:00:00+08:00","reason":"failure"}'
```

**6. 结算班次**

```bash
curl -X POST http://localhost:8000/shifts/1/settle
```

```json
{
  "settled": true,
  "result": {
    "shift_id": 1, "revision": 1, "latest_revision": 1,
    "totals": {"maintenance": 60, "failure": 60, "pending": 0},
    "delta":  {"maintenance": 60, "failure": 60, "pending": 0},
    "event_versions": [
      {"source_id": "MES-1", "version": 1, "reason": "maintenance"},
      {"source_id": "PLC-9", "version": 1, "reason": "failure"}
    ],
    "trigger": "settle", "..."
  }
}
```

> 10:30–11:00 与保养重叠，按优先级归保养；故障只计 11:00–12:00 的 60 分钟。

**7. 迟到更正**：故障实际只到 11:30，上报 `PLC-9` 的版本 2

```bash
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"device_id":"CNC-01","source_id":"PLC-9","version":2,
       "start":"2026-10-01T10:30:00+08:00","end":"2026-10-01T11:30:00+08:00","reason":"failure"}'
```

```json
{
  "version": 2, "idempotent": false,
  "recomputed": [{
    "shift_id": 1, "revision": 2,
    "totals": {"maintenance": 60, "failure": 30, "pending": 0},
    "delta":  {"maintenance": 0,  "failure": -30, "pending": 0}
  }]
}
```

**8. 查询结果与修订**

```bash
curl http://localhost:8000/shifts/1/result                # 最新修订（revision=2）
curl 'http://localhost:8000/shifts/1/result?revision=1'   # 指定修订：结算时的原结果（failure=60）
curl http://localhost:8000/shifts/1/revisions             # 全部修订及每次差额
```

**9. 现场人工裁定**（仅对已结算班次；接续第 8 步，当前最新为修订 2）

```bash
# 9.1 提交裁定：10:30–11:00 现场改判为"待判"（原被保养覆盖）
curl -X POST http://localhost:8000/shifts/1/adjudications \
  -H 'Content-Type: application/json' \
  -d '{"ruling_no":"R-20261001-001",
       "start":"2026-10-01T10:30:00+08:00","end":"2026-10-01T11:00:00+08:00",
       "reason":"pending","note":"传感器无记录，待质检复核"}'
```

```json
{
  "ruling_no": "R-20261001-001",
  "status": "active",
  "idempotent": false,
  "revision": 3,
  "latest_revision": 3,
  "revision_detail": {
    "revision": 3, "seq": 3,
    "totals": {"maintenance": 30, "failure": 30, "pending": 30},
    "delta":  {"maintenance": -30, "failure": 0,  "pending": 30},
    "trigger": "adjudication"
  }
}
```

> 裁定分钟绝对覆盖事件归因：10:30–11:00 原本归保养，现改判待判。
> 分钟数无变化时（如把本就是故障的分钟再裁定为故障），仍落库但 **不产生修订**，
> 回执为 `"idempotent": false, "revision": null, "revision_detail": null, "latest_revision": N`。

```bash
# 9.2 同单号同内容重试 → 幂等 200，无修订明确标示
curl -X POST http://localhost:8000/shifts/1/adjudications -H 'Content-Type: application/json' \
  -d '{"ruling_no":"R-20261001-001",
       "start":"2026-10-01T10:30:00+08:00","end":"2026-10-01T11:00:00+08:00",
       "reason":"pending","note":"传感器无记录，待质检复核"}'
# 200 {"ruling_no":"R-20261001-001","status":"active","idempotent":true,
#      "revision":null,"latest_revision":3,"revision_detail":null}

# 9.3 同单号异内容 → 409 RULING_CONFLICT
curl -X POST http://localhost:8000/shifts/1/adjudications -H 'Content-Type: application/json' \
  -d '{"ruling_no":"R-20261001-001",
       "start":"2026-10-01T10:30:00+08:00","end":"2026-10-01T11:00:00+08:00",
       "reason":"failure","note":"改判故障"}'
```

```bash
# 9.4 更晚生效的重叠裁定：10:45–11:15 改判故障，与上一条重叠的 10:45–11:00 以本条为准
curl -X POST http://localhost:8000/shifts/1/adjudications -H 'Content-Type: application/json' \
  -d '{"ruling_no":"R-20261001-002",
       "start":"2026-10-01T10:45:00+08:00","end":"2026-10-01T11:15:00+08:00",
       "reason":"failure","note":"现场确认为传动故障"}'
# → revision=4；重叠 15 分钟从 pending 变为 failure，11:00–11:15 的事件故障本就计 failure，不变

# 9.5 按单号撤销：重叠分钟回退至此前仍有效的 R-...001（pending），产生 adjudication_revoke 修订
curl -X POST http://localhost:8000/shifts/1/adjudications/R-20261001-002/revoke
# → {"status":"revoked","idempotent":false,"revision":5,
#    "revision_detail":{"trigger":"adjudication_revoke", ...}}

# 重复撤销 → 200 幂等，revision=null；未知单号 → 404 RULING_NOT_FOUND
curl -X POST http://localhost:8000/shifts/1/adjudications/R-20261001-002/revoke

# 9.6 查看班次全部裁定（含已撤销，按生效顺序）
curl http://localhost:8000/shifts/1/adjudications
```

裁定生效期间若有事件迟到更正，重算同样遵守裁定：裁定分钟不被事件改动，
事件只影响未被裁定覆盖的分钟，差额照常进入修订与 `/changes`。

**10. 错误示例**

```bash
# 非法区间（start ≥ end、秒非 00、缺时区）→ 422
curl -X POST http://localhost:8000/shifts -H 'Content-Type: application/json' \
  -d '{"device_id":"CNC-01","start":"2026-10-02T16:00:00+08:00","end":"2026-10-02T08:00:00+08:00"}'

# 未知设备 → 404 DEVICE_NOT_FOUND
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"device_id":"NOPE","source_id":"X","version":1,
       "start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:00:00+08:00","reason":"failure"}'
```

## 变更同步（下游报表订阅）

`GET /changes` 把每次**结算**与每次**导致结果变化的更正 / 现场裁定 / 撤销**暴露为按全局
递增序号 `seq` 排序的变更流，供下游报表持续同步。每条变更包含：班次（id、设备、起止）、
修订号、各原因分钟数（`totals`）、与上一修订的差额（`delta`）、触发类型（`trigger`：
`settle` / `correction` / `adjudication` / `adjudication_revoke`）、所依据的事件版本与
产生时间。

**游标语义**

- 请求参数 `cursor` 表示"已消费的最大 `seq`"，返回 `seq > cursor` 的变更，按 `seq` 升序；
  `cursor=0`（或缺省）表示从头回放全部既有修订。
- 每页返回 `next_cursor`（本页最后一条的 `seq`），作为下一次请求的 `cursor` 即可翻页。
- **空页保持原游标**：已追平时返回 `{"changes": [], "next_cursor": <原 cursor>}`，
  客户端可用同一游标轮询等待新变更。
- 页大小 `limit` 缺省 100，上限 500。

**一致性保证**

- 变更记录与对应修订是同一行（`shift_results.seq`），同一事务提交或回滚，不会半截可见；
- 序号由单例计数器在写事务内加锁分配，**序号顺序 == 提交顺序**：并发结算/更正时，
  游标不会越过尚未可见的较早变更；
- 条目只追加不修改，重读已返回的内容不变；
- 幂等重复上报/结算、结果未变化的更正、分钟数无变化的裁定/撤销均**不产生**变更记录。

**参数约束**：`cursor ≥ 0` 且不得超过当前最大序号；`1 ≤ limit ≤ 500`。
非法游标或超限页大小返回 `422`（只读请求，不影响任何同步进度）。

**调用示例**（接上面主流程，此时已有结算 seq=1、更正 seq=2 两条变更）

```bash
# 1) 从头回放：第一页（每页 1 条）
curl 'http://localhost:8000/changes?cursor=0&limit=1'
```

```json
{
  "changes": [{
    "seq": 1, "shift_id": 1, "device_id": "CNC-01",
    "shift_start": "2026-10-01T00:00:00Z", "shift_end": "2026-10-01T08:00:00Z",
    "revision": 1,
    "totals": {"maintenance": 60, "failure": 60, "pending": 0},
    "delta":  {"maintenance": 60, "failure": 60, "pending": 0},
    "event_versions": [
      {"source_id": "MES-1", "version": 1, "reason": "maintenance"},
      {"source_id": "PLC-9", "version": 1, "reason": "failure"}
    ],
    "trigger": "settle", "created_at": "..."
  }],
  "next_cursor": 1
}
```

```bash
# 2) 用上一页的 next_cursor 翻页
curl 'http://localhost:8000/changes?cursor=1&limit=100'
# → changes[0] 为 seq=2 的更正（trigger="correction"，delta.failure=-30），next_cursor=2

# 3) 已追平：空页保持原游标，可轮询等待
curl 'http://localhost:8000/changes?cursor=2'
# → {"changes": [], "next_cursor": 2}

# 4) 非法参数 → 422，同步进度不受影响
curl 'http://localhost:8000/changes?cursor=-1'     # VALIDATION_ERROR
curl 'http://localhost:8000/changes?limit=1000'    # VALIDATION_ERROR（上限 500）
curl 'http://localhost:8000/changes?cursor=999'    # INVALID_CURSOR（超过当前最大序号）
```

## 设计要点

- **归因算法**（`app/attribution.py`，纯标准库可独立测试）：按原因收集区间并裁剪到班次窗口；
  保养区间合并后全量生效，故障区间扣除保养覆盖，待判区间再扣除保养与故障覆盖 —— 区间运算
  保证每分钟只归因一次；`used_versions` 只保留裁剪后仍对有效区间有贡献的事件版本。
  现场裁定（`attribute_with_rulings`）逐分钟取生效顺序（seq，即落库 id）最新的未撤销裁定，
  裁定分钟绝对覆盖事件；事件只在扣除全部裁定区间后的剩余窗口内做上述优先级归因，
  因此撤销最新裁定后重叠分钟自然回退到此前裁定或事件。
- **版本链与幂等**：`event_sources` 表保存每个 `(设备, 来源)` 的 `last_version` 与内容哈希，
  上报时 `INSERT ... ON CONFLICT DO NOTHING` + `SELECT ... FOR UPDATE` 串行化校验；
  内容哈希 = `sha256(start_utc|end_utc|reason)`。
- **裁定与撤销**：`manual_adjudications` 保存裁定原文与 `active/revoked` 状态（撤销留痕、
  行不删除），`(shift_id, ruling_no)` 唯一；内容哈希 = `sha256(start_utc|end_utc|reason|note)`，
  同单号同内容幂等、异内容冲突（含复用已撤销单号）。提交/撤销在**班次行 `FOR UPDATE` 锁内**
  完成，故同班次的结算、事件更正重算与所有裁定写入相互串行，裁定 id 顺序即对其他事务可见的
  生效顺序；并发不丢失更新。
- **迟到更正**：新版本落库后，按"旧版本区间 ∪ 新版本区间"找出受影响的已结算班次，用全部
  来源的最新版本与当前生效裁定重算；仅当分钟数变化时追加新修订（避免空修订），差额 = 新 − 旧。
- **并发与一致性**：班次不重叠由 PostgreSQL 排他约束（`btree_gist` + `tstzrange '[)'`）兜底；
  结算、更正重算与裁定写入对班次行加锁；事件版本链对来源行加锁，全流程单事务提交。
- **不可变历史**：`shift_results` 只追加不修改，`(shift_id, revision)` 唯一；裁定撤销只改
  自身状态并追加修订，从不改写历史修订与已返回的变更条目。
- **变更订阅**：变更流直接读取修订表 —— `shift_results.seq` 即全局变更序号，修订与变更
  天然同事务提交/回滚；序号由单例计数器行（`change_counter`）在写事务内
  `UPDATE ... RETURNING` 递增分配，行锁持有至提交，故序号顺序 == 提交顺序，同步游标不会越过
  尚未可见的较早变更；条目不可变，重读一致。既有库启动时自动迁移：为 `shift_results`
  回填 `seq`（按原自增 id 顺序）、加宽 `trigger` 列以容纳裁定触发器，并以最大序号初始化计数器。

## 项目结构

```
app/
  main.py         # FastAPI 路由
  services.py     # 业务逻辑：版本链、幂等、结算、更正重算
  attribution.py  # 归因算法（半开区间运算，纯标准库）
  models.py       # SQLAlchemy 模型
  schemas.py      # Pydantic 请求/响应与校验
  db.py           # 引擎、会话、建库建表（含排他约束）
  config.py       # 环境变量配置
tests/
  test_attribution.py    # 算法单测（含裁定覆盖/重叠/回退，无外部依赖，可 python3 直接跑）
  test_api.py            # API 端到端测试（需 PostgreSQL）
  test_changes.py        # 变更同步（游标分页/回放/并发）端到端测试（需 PostgreSQL）
  test_adjudications.py  # 现场裁定（提交/幂等/冲突/撤销/联动/并发）端到端测试（需 PostgreSQL）
scripts/smoke.sh         # 冒烟脚本
Dockerfile / docker-compose.yml / requirements.txt
```

## 本地开发（不用 Docker）

```bash
pip install -r requirements.txt
export DATABASE_URL='postgresql+psycopg://postgres:postgres@localhost:5432/downtime'
uvicorn app.main:app --reload --port 8000
pytest
```
