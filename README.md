# 生态监测名称归并供数 API

把野外观测记录中的旧物种名（同义名）沿分类表的指向关系追踪到**接受名**，生成可重读、
可追溯修订的供数结果。技术栈：FastAPI + SQLite，本地单容器部署。

## 业务规则

### 观测批次
- `POST /batches` 一次录入一批观测，**至多 100 条**，每条含批内唯一编号 `code` 与物种名 `name`。
- 批内编号重复返回 `409`，且整批回滚、不留部分数据。

### 分类表修订（可整版更正）
- `POST /taxonomies` 整版提交分类表：接受名列表 + **至多 40 条**同义名指向关系。
- 同义关系为有向边（`synonym → target`），**允许分叉**（一个名称指向多个目标），也允许链式与菱形合流。
- 每次提交生成一个新的不可变修订；旧修订标记 `superseded`，引用旧修订的有效结果立即置为 `expired`。

### 名称追踪与供数判定
对每条观测的名称，沿所有可达路径追踪：

| 情况 | 处理 | reason_code |
|---|---|---|
| 所有路径无环且汇聚到**同一个**接受名 | 纳入供数 `included`，记录接受名 | — |
| 不同路径通向不同接受名（分叉冲突） | 待核 `pending` | `divergent_accepted` |
| 可达子图中存在环（含自环） | 待核 `pending` | `cycle` |
| 没有可达的接受名（含部分路径断在非接受名） | 待核 `pending` | `no_accepted_name` |

### 结果
- `POST /results` 生成结果，**固定观测批次与分类表修订**，返回结果号 `result_id`。
- `GET /results/{result_id}` 按结果号重读；分类表更正后旧结果标 `expired` 但内容冻结、仍可读取。
- 生成与更正交错时：写操作经 SQLite `BEGIN IMMEDIATE` 串行化；针对旧修订生成的结果直接落为
  `expired`，**旧修订永远不会覆盖当前供数**；任何失败整体回滚，不产生部分结果。
- 同一批次新生成的当前结果会使该批次旧的 `active` 结果过期，保证每批次至多一份当前供数。

### 人工核定（不改分类关系）
复核员拿到鉴定证据后，对单条待核观测做人工核定，**只决定该观测采纳哪个终点，不写入也不修改
任何同义名指向关系**。

- `POST /confirmations` 提交当前结果号 `result_id`、观测编号 `observation_code`、候选接受名
  `accepted_name` 与依据 `basis`。
- **可核定范围**：仅受理「无环且所有路径均终止于接受名的分叉」待核记录（即
  `divergent_accepted` 且无任何非接受名终点）；环（`cycle`）、无接受名（`no_accepted_name`）、
  已纳入记录一律拒绝（409）。候选接受名必须是该观测分叉终点之一，否则 409。
- **只收当前结果**：新核定只允许提交给 `active` 结果；对 `expired` 结果提交返回 409。
- **幂等与冲突**：同结果、同观测的同内容（候选名与依据均相同）重试返回原核定（HTTP 200，
  不新建）；异内容返回 **409**，已存核定不可改写。
- **同名其他观测不受影响**：核定按「批次 + 观测编号」绑定，其他批次/记录中的同名观测不变化。
- **后续供数采用有效核定**：新生成结果时，仍属有效且终点集合匹配的核定被套用，对应观测纳入
  供数（`reason_code=manual_confirmation`），结果条目保存**核定号与依据**；旧结果内容冻结、
  **不改写**。
- **更正导致的永久失效**（在提交更正的同一事务内重算并落库）：
  - 更正改变了该观测名称的**终点接受名集合** → `terminal_set_changed`；
  - 更正后该名称的可达子图**引入环** → `cycle_introduced`；
  - 更正后出现**非接受名终点** → `non_accepted_terminal`。
  - 失效为**永久性**：之后即使改回原结构也不复活。更正未影响该观测（终点集合不变且仍无环、
  全接受名终点）时核定保留。
- 核定状态可经 `GET /confirmations`（可按 `result_id`、`status=valid|invalidated` 过滤）与
  `GET /confirmations/{id}` 查询，失效记录带失效原因与时间。
- 更正、核定与生成交错时，三者均经同一 `BEGIN IMMEDIATE` 写锁串行，核定在写事务内重新确认
  结果状态与解析结论，归属始终对应同一已提交状态；任何失败整体回滚，不留部分核定或部分结果。

### 旧版本 SQLite 卷兼容
应用启动时自动执行幂等迁移：旧卷缺少 `manual_confirmation` 表及 `result_item` 的
`confirmation_id` / `confirmation_basis` 列时自动补齐；历史结果的核定字段为空（`null`），
无核定供数的读取与生成行为与旧版本完全一致。

## 启动方式

### Docker Compose（推荐）

```bash
docker compose up --build
# 打开交互文档
open http://localhost:8000/docs   # Linux 用 xdg-open
```

SQLite 数据保存在命名卷 `eco_data`（容器内 `/data/eco.db`），容器重建不丢数据。

### 本地直接运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 可通过环境变量指定库路径：ECO_DB_PATH=/path/to/eco.db
```

## /docs 操作流程（Swagger UI）

1. `POST /batches` 录入观测批次；
2. `POST /taxonomies` 提交分类表（更正时再次调用即可，自动出新修订）；
3. `POST /results` 生成供数结果；
4. 对分叉待核观测，在 `POST /confirmations` 填入当前结果号、观测编号、候选接受名与鉴定依据；
5. 再次 `POST /results` 生成后续供数，套用有效核定（条目带核定号与依据）；
6. `GET /results/{result_id}` 查询明细，`GET /confirmations/{id}` 读取核定依据与失效状态，
   或 `GET /results?status=active` 查看当前供数。

## curl 示例

```bash
# 1. 录入观测（6 条，覆盖纳入/分叉/环/无接受名/合流/直达）
curl -s -X POST localhost:8000/batches -H 'Content-Type: application/json' -d '{
  "note": "第一批",
  "observations": [
    {"code": "o1", "name": "旧名甲"},
    {"code": "o2", "name": "分叉名"},
    {"code": "o3", "name": "环名x"},
    {"code": "o4", "name": "无人名"},
    {"code": "o5", "name": "菱形名"},
    {"code": "o6", "name": "接受名A"}
  ]}'

# 2. 提交分类表（10 条边，含分叉与环）
curl -s -X POST localhost:8000/taxonomies -H 'Content-Type: application/json' -d '{
  "note": "v1",
  "accepted_names": ["接受名A", "接受名B"],
  "synonym_edges": [
    {"synonym": "旧名甲", "target": "中间名"},
    {"synonym": "中间名", "target": "接受名A"},
    {"synonym": "分叉名", "target": "接受名A"},
    {"synonym": "分叉名", "target": "接受名B"},
    {"synonym": "环名x", "target": "环名y"},
    {"synonym": "环名y", "target": "环名x"},
    {"synonym": "菱形名", "target": "s1"},
    {"synonym": "菱形名", "target": "s2"},
    {"synonym": "s1", "target": "接受名A"},
    {"synonym": "s2", "target": "接受名A"}
  ]}'

# 3. 生成供数结果（revision_id 缺省取最新修订）
curl -s -X POST localhost:8000/results -H 'Content-Type: application/json' \
  -d '{"batch_id": 1}'

# 4. 按结果号重读
curl -s localhost:8000/results/1

# 5. 对分叉待核观测 o2 做人工核定（候选名必须是分叉终点 A/B 之一）
curl -s -X POST localhost:8000/confirmations -H 'Content-Type: application/json' -d '{
  "result_id": 1,
  "observation_code": "o2",
  "accepted_name": "接受名B",
  "basis": "标本复核：形态与模式标本一致，采纳接受名B"
}'

# 6. 再次生成供数：o2 按有效核定纳入，条目带 confirmation_id 与依据
curl -s -X POST localhost:8000/results -H 'Content-Type: application/json' \
  -d '{"batch_id": 1}'

# 7. 查询核定（含失效原因）；可加 ?result_id=1 或 ?status=valid 过滤
curl -s localhost:8000/confirmations
curl -s localhost:8000/confirmations/1
```

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/batches` | 录入观测批次（≤100 条） |
| GET | `/batches` | 批次列表 |
| GET | `/batches/{id}` | 批次明细 |
| POST | `/taxonomies` | 整版提交分类表，生成新修订（边 ≤40） |
| GET | `/taxonomies` | 修订列表 |
| GET | `/taxonomies/{id}` | 修订完整内容 |
| POST | `/results` | 生成供数结果 |
| GET | `/results` | 结果列表（可按 `batch_id`、`status` 过滤） |
| GET | `/results/{id}` | 按结果号重读（含每条观测的追踪结论、原因及套用的核定号/依据） |
| POST | `/confirmations` | 提交人工核定（同内容重试 200 幂等，异内容 409，仅当前结果受理） |
| GET | `/confirmations` | 核定列表（可按 `result_id`、`status=valid/invalidated` 过滤） |
| GET | `/confirmations/{id}` | 按核定号读取核定内容、依据与永久失效原因 |

## 测试

```bash
pip install pytest httpx
python -m pytest tests/ -q
```

- `tests/test_resolver.py`：解析算法（链、分叉合流、分叉冲突、环、断链等 11 个用例）；
- `tests/test_api.py`：端到端业务场景（更正过期、旧修订不得激活、上限校验、404、结果冻结等）；
- `tests/test_concurrency.py`：4 线程 100 次生成/更正交错，校验无部分结果、至多一份当前供数、
  active 必属最新修订、结果与串行重算一致；
- `tests/test_confirmation.py`：人工核定全场景（可核定范围、同内容幂等/异内容 409、仅当前结果、
  更正致终点集合/环/非接受名终点失效且不复活、无关更正保留、同名其他观测不受影响、旧 SQLite 卷
  迁移与无核定供数兼容）；
- `tests/test_confirmation_concurrency.py`：4 线程 120 次更正/生成/核定交错，校验无部分写入、
  至多一份当前供数、失效判定完整、供数引用的核定号必为有效核定。

## 目录结构

```
app/
  database.py    # SQLite 连接、建表、旧卷幂等迁移、IMMEDIATE 写事务
  models.py      # Pydantic v2 请求/响应模型与上限校验
  resolver.py    # 名称追踪：可达性 + Kahn 环检测 + 汇聚判定 + 可核定终点集合
  services.py    # 事务化业务服务（批次/修订/结果/人工核定/失效重算）
  main.py        # FastAPI 路由
tests/           # pytest 测试
Dockerfile, docker-compose.yml, requirements.txt
```
