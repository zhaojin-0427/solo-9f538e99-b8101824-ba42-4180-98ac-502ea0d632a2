# 地震波形片段拼接服务（Waveform Assembly API）

把乱序到达的波形片段按 **台站 + UTC 分钟** 收集、校验、拼装，
仅对 **完整且无冲突** 的分钟生成不可变的发布记录，供震相识别程序取用。

- 框架：FastAPI（自带 `/docs` Swagger UI、`/redoc`）
- 存储：SQLite（WAL 模式，单文件，挂在 `/data` 卷上，重启数据不变）
- 并发：所有写操作走 `BEGIN IMMEDIATE` 事务，上传与发布交错时在同一已提交片段集合上串行执行

## 一、启动

### Docker Compose（推荐）

```bash
docker compose up --build
# API:    http://localhost:8000
# 文档:   http://localhost:8000/docs
# 健康:   http://localhost:8000/health
```

数据库保存在命名卷 `waveform-data`（容器内 `/data/waveform.db`）。

### 本地直接运行

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
WAVEFORM_DB_PATH=./data/waveform.db uvicorn app.main:app --reload
```

## 二、数据模型与规则

片段（fragment）：

| 字段 | 约束 |
| --- | --- |
| `station` | 非空字符串，台站代码 |
| `minute_utc` | 整分钟 UTC 时间（秒必须为 0），如 `2026-11-17T05:06:00Z` |
| `fragment_no` | 片段号，非负整数 |
| `revision` | 修订号，**正整数** |
| `start_sample` | 起始采样位，0–59 |
| `samples` | 非空整数数组；`start_sample + len(samples) ≤ 60`，越界拒绝（422） |

修订与重传规则（同一 `station + minute_utc + fragment_no`）：

- 修订号 **更高** → 接受并覆盖旧版（`status: "superseded"`）
- 修订号 **更低** → 拒绝（409），旧数据不动
- 同修订号、**同内容**（SHA-256 一致）→ 幂等重传，沿用原结果（`status: "unchanged"`），不产生新行
- 同修订号、**异文** → 拒绝（409），旧数据不动

拼装（assembly）：

- 每分钟固定 60 个 1 Hz 采样位
- 缺位在 `gaps` 中逐个列出；覆盖数见 `coverage`
- 重叠位数值不同即为冲突，`conflicts` 给出 **位置、涉及片段号、各自数值**；
  重叠但数值相同不算冲突
- `complete = 60 位全覆盖且无冲突`

发布（publication）：

- 只有完整且无冲突的分钟可发布；缺位或冲突时返回 409 并在 `detail` 中说明，
  **失败不留下任何半份记录**（单事务回滚）
- 发布记录冻结当时的 60 点波形 `wave`、每个片段的修订号与内容（`items`），
  以及片段集合指纹 `fragment_set_hash`
- 同一片段集合重复发布是幂等的，返回同一条记录
- 之后补传/更正只改变当前拼装；该分钟片段集合一旦变化，旧记录自动标 `stale=true`，
  仍可按 ID 读取，波形与修订保持冻结
- 每份记录只来自同一个已提交片段集合（事务快照保证）

## 三、HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/fragments` | 上传片段（JSON body） |
| GET | `/fragments?station=&minute_utc=` | 查看某分钟当前片段 |
| GET | `/assembly?station=&minute_utc=` | 查看拼装状态：覆盖率、缺口、冲突 |
| POST | `/publications?station=&minute_utc=` | 发布完整无冲突分钟 |
| GET | `/publications?station=&minute_utc=` | 发布记录列表（可按台站/分钟过滤） |
| GET | `/publications/latest?station=&minute_utc=` | 该分钟最新一条记录 |
| GET | `/publications/{id}` | 按 ID 读取（过期记录同样可读） |
| GET | `/trace?station=&minute_utc=&seq=` | **追溯**：重建该序号时刻的片段、缺位、冲突与失效归因 |
| GET | `/trace?station=&minute_utc=&fragment_no=` | **追溯**：某片段最近一次变更的快照 |
| GET | `/trace/events?station=&minute_utc=` | **追溯**：该分钟完整、按序的变化账本 |

原有 8 个接口的路径、参数与响应结构保持不变（仅新增 `/trace*`）。

## 三之二、拼接质量追溯

### 记录哪些变更

以下情况会在**同一事务**内追加一条不可变事件（`trace_events`，全局递增 `seq`）：

| 事件 `kind` | 触发条件 | 携带内容 |
| --- | --- | --- |
| `fragment_added` | 新的 `fragment_no` 首次被接受 | 新片段、变更后的完整片段集合 |
| `fragment_revised` | 同一 `fragment_no` 收到**更高**修订并被接受 | 新修订、旧修订（供对照）、变更后的完整片段集合 |
| `publish` | 成功发布一条记录 | 被冻结的片段集合 |
| `baseline` | 仅升级旧库时，每分钟一条 | 升级瞬间的片段现态 |

同版同文重传（`status: "unchanged"`）与所有失败上传/发布（409/422）**都不写事件**，
序号不前进；失败事务整体回滚，不留半份事件、发布或失效记录。

### 重建规则

每个事件都保存变更后的**完整有效片段集合快照**，所以：

```
状态(seq) = seq 及之前最后一条已提交事件的快照
```

- `seq` 省略 → 取该分钟最近一次已接受的片段变更（不含 `publish`）。
- `seq` 落在两条事件之间 → 取不晚于它的最后一条事件，`resolved_seq` 回报实际使用的序号。
- `seq` 早于该分钟第一条事件、但晚于历史起点 → 该分钟当时必然为空，返回空集合、
  `coverage=0` 的拼装与空发布列表（`state_known=true`）。
- `seq` 早于历史起点（升级前的旧数据）→ `state_known=false` 且给出 `unknown_reason`，
  **不猜测**。

历史快照不随后续更正改写。后续更正只会新增事件；旧 `seq` 仍返回旧修订、旧缺位/冲突，
并在片段上以 `superseded_later` / `superseded_at_seq` **附加**标注它后来被替换。

### 缺位、冲突与失效归因

- `assembly.gaps`：60 个采样位中无人覆盖的位置（样本级缺位）。
- `assembly.conflicts`：重叠位置数值不一致，给出位置、涉及片段号与各自数值。
- `assembly.missing_fragment_nos`：片段编号本身的空洞（如只有 0、2 号片段），
  与采样位缺位区分开。
- `expired_publications`：**本次所选变更**使其失效的发布，含该发布的
  `invalidated_by_seq` 与 `invalidated_at`。归因在变更提交时写入
  `publication_invalidations`，与片段变更同一事务，因此总是指向同一已提交状态。
- `publications`：在所选 `seq` 时刻仍然有效、且片段集合与当时一致的发布。

### 示例

```bash
# 该分钟完整变化账本
curl -s "localhost:8000/trace/events?station=STA1&minute_utc=$MIN" | jq

# 定位某次更正当时的状态，以及它让哪些发布过期
curl -s "localhost:8000/trace?station=STA1&minute_utc=$MIN&seq=4" | jq \
  '{fragments: [.fragments[] | {fragment_no, revision}],
    gaps: .assembly.gaps, conflicts: .assembly.conflicts,
    expired: [.expired_publications[].id]}'

# 某片段最近一次被接受的是哪一版
curl -s "localhost:8000/trace?station=STA1&minute_utc=$MIN&fragment_no=1" | jq .event
```

### 升级已有 SQLite 卷

`init_db()` 在启动时自动迁移，无需手工操作：

1. 为 `publications` 追加 `created_event_seq`、`creation_time_known`、
   `invalidated_event_seq` 列；新建 `trace_events`、`publication_invalidations`、
   `trace_meta` 表。
2. 对每个已有 `station + minute_utc` 写入一条 `baseline` 事件，快照即**升级瞬间的片段现态**。
3. 已有发布记录挂在基线之后，`creation_time_known=false`、`created_event_seq=null`：
   它们在升级前存在，但写入时刻与曾使其失效的变更**未曾保存，明确标为未知**。
4. 迁移只执行一次（`trace_meta` 守卫），重复启动不会重复写基线，发布记录 ID 不变。

升级后：

- 查询基线及之后 → 正常返回，`unknown_before_baseline=true`。
- 查询基线之前 → `state_known=false`，绝不伪造历史。
- 升级后新出现的台站分钟从 0 起即完整已知。

### 并发与一致性

上传、发布与追溯读取共用 SQLite 的 `BEGIN IMMEDIATE` 写事务；追溯读取使用显式只读
事务（`BEGIN` … `ROLLBACK`），保证一次响应内的历史快照、拼装结果与失效归因来自
**同一个已提交状态**，不会读到半提交或跨状态拼接的结果。

### 典型流程

```bash
MIN=2026-11-17T05:06:00Z
# 乱序上传两个片段，恰好铺满 60 位
curl -s -X POST localhost:8000/fragments -H 'content-type: application/json' -d '{
  "station":"STA1","minute_utc":"'"$MIN"'","fragment_no":2,"revision":1,
  "start_sample":30,"samples":'"$(/workspace/.venv/bin/python -c 'print(list(range(30,60)))')"'}'
curl -s -X POST localhost:8000/fragments -H 'content-type: application/json' -d '{
  "station":"STA1","minute_utc":"'"$MIN"'","fragment_no":1,"revision":1,
  "start_sample":0,"samples":'"$(/workspace/.venv/bin/python -c 'print(list(range(0,30)))')"'}'

# 检查拼装
curl -s "localhost:8000/assembly?station=STA1&minute_utc=$MIN"
# 发布
curl -s -X POST "localhost:8000/publications?station=STA1&minute_utc=$MIN"
```

## 四、测试与自测

```bash
pip install pytest httpx
python -m pytest tests/ -v          # 43 个用例：校验/修订/拼装/发布/过期/重启/追溯/升级/并发
python scripts/smoke.py             # 对运行中的服务做 41 项端到端检查
```

测试覆盖的关键行为：

- **追溯**：事件序号递增；任一序号可重建当时的修订集合、缺位与冲突；后续更正
  即使消除冲突，也不改写旧序号的结果；失效归因指向具体变更；幂等重传与失败请求不写事件。
- **升级**：用 v1.0 旧表结构真实造库，验证基线以升级现态为起点、旧发布
  `creation_time_known=false`、基线前查询为 `state_known=false`、重复迁移幂等。
- **并发**：启动真实 uvicorn 进程，多线程交错上传/发布/追溯；断言追溯快照与自身
  拼装一致、失效归因与发布 `stale` 标志一致、完成态恰好一条完整发布且无半份数据。

## 五、目录结构

```
app/
  config.py      # 环境变量配置（WAVEFORM_DB_PATH）
  database.py    # SQLite 连接、建表/升级迁移、只读快照事务
  assembly.py    # 纯函数：规范化、快照哈希、缺位/冲突算法
  models.py      # Pydantic 请求/响应模型（含追溯模型）
  service.py     # 事务、上传、拼装、发布、过期归因与追溯重建
  main.py        # FastAPI 路由
tests/test_api.py
scripts/smoke.py
Dockerfile
docker-compose.yml
requirements.txt
```
