# 野外调查观测更正汇交 API

多台离线设备对同一观测的更正在此汇交，供生态分析取数。FastAPI + SQLite，
两个 API 进程共库运行，任一端写入、两端查询结论一致，重启保留。

## 数据模型与状态机

每条更正是观测修订 DAG 中的一个节点：

| 字段 | 含义 |
|---|---|
| `observation_id` | 观测号 |
| `revision_id` | 全局唯一修订号 |
| `parents` | 直接父修订号列表（空数组 = 根修订） |
| `species` + `count` | 改值：物种与数量 |
| `delete: true` | 删除标记（与物种/数量互斥） |

每个观测由已到达的修订自动重算出三种状态：

| 状态 | 含义 | 有效值 |
|---|---|---|
| `pending` | 祖先未齐（有父修订尚未到达，允许子先父后） | 不输出 |
| `valid` | 祖先齐全且只剩一个无后继修订（当前分支唯一） | 顶端修订的载荷（删除也是合法结论） |
| `conflict` | 祖先齐全但有多个无后继修订（多分支；改值与删除并存同属此类） | 不输出 |

- **乱序到达**：子先父后时观测标 `pending` 并列出 `missing_ancestors`，父补齐后自动重算。
- **人工裁决**：`POST /observations/{id}/resolutions`，以裁决时所见全部分支（`tips`）为父新建修订；
  提交时分支已变则返回 **409** 及当前分支；裁决之后迟到的并发分支会重新形成冲突，不会被吞没。
- **幂等**：同号同内容重传原样接受（记入 `duplicates`，状态不变）；同号异内容整包拒绝。
- **整包拒绝（422）不留部分记录**：同号异内容、循环引用（含自引用）、跨观测引用
  （含父修订后到但观测号不符的反向情形）。
- **原子性**：一个包可含多条修订、多个观测，任一非法则整体回滚。

## 物种占比判读

`POST /assessments`：输入最多 50 个不重复观测号、目标物种、0～100 的整数百分比阈值，
判断目标物种个体数占整批总数的比例是否**严格大于**预警线。

- 每条冲突观测只取一条当前分支（`tips` 之一）；`valid` 观测只有一条分支，实际被强制。
- 目标物种个体数与整批总数始终来自**同一组**分支选择，不会混用不同选择。
- 删除分支两项均计 **0**；非目标物种只计入整批总数。
- 结果：
  - `确定超标` / `exceeds`：所有可行选择都超标；
  - `确定未超` / `not_exceeds`：所有可行选择都未超标；
  - `待裁决` / `undecided`：有的选择超标、有的未超标，`witnesses` 给出各一组
    支持超标与未超标的修订号（含每条选择的分项计数、总数、占比）；
  - 不给判定 / `blocked`：`verdict` 为 `null`，`reasons` 列出全部原因 ——
    观测不存在、观测待补齐（附未齐祖先）、存在某种选择使整批总数为零。
- 判读在一个只读快照内读取观测状态与分支载荷，双进程汇交期间，
  判定与所用分支必然来自同一个已提交状态（不会出现“新分支 + 旧结论”）。

判定用可加分值 `100 × 目标物种数 − 阈值 × 总数` 表示；每条分支分值固定，
逐观测取最大/最小再求和，即可覆盖全部可行选择（分支组合数指数增长时仍为线性），
`max > 0` 说明存在超标选择，`min > 0` 说明所有选择都超标。

## 运行（Docker Compose）

```bash
docker compose up --build
```

- api-a: http://localhost:8001/docs （Swagger UI，可上报、查冲突、裁决、判读）
- api-b: http://localhost:8002/docs （与 api-a 共库，结论一致）
- 数据存于命名卷 `survey-data`（SQLite 文件），`docker compose down` 后重启保留；
  `docker compose down -v` 才会清空。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/packages` | 上报 JSON 包：`{"revisions": [...]}`，整体原子 |
| `GET` | `/observations` | 列出全部观测状态，可 `?status=pending\|valid\|conflict` 过滤 |
| `GET` | `/observations/{id}` | 单个观测：状态、当前分支、未齐祖先、有效值 |
| `GET` | `/observations/{id}/revisions` | 观测的全部修订（DAG 节点） |
| `GET` | `/conflicts` | 查冲突：全部多分支观测及其当前分支 |
| `POST` | `/assessments` | 物种占比判读：确定超标 / 确定未超 / 待裁决（附见证分支）/ 不给判定（列原因） |
| `POST` | `/observations/{id}/resolutions` | 人工裁决（201；幂等重放 200；分支已变 409） |
| `GET` | `/health` | 健康检查 |

### 上报示例

```bash
curl -X POST http://localhost:8001/packages -H 'Content-Type: application/json' -d '{
  "revisions": [
    {"observation_id": "OBS-1", "revision_id": "r1", "parents": [],
     "species": "Cervus elaphus", "count": 3},
    {"observation_id": "OBS-1", "revision_id": "r2", "parents": ["r1"],
     "delete": true}
  ]
}'
```

### 判读示例

```bash
curl -X POST http://localhost:8001/assessments -H 'Content-Type: application/json' -d '{
  "observation_ids": ["OBS-A", "OBS-B"],
  "species": "Cervus elaphus",
  "threshold_percent": 50
}'
```

### 裁决示例

```bash
curl -X POST http://localhost:8001/observations/OBS-1/resolutions \
  -H 'Content-Type: application/json' -d '{
    "revision_id": "r3", "parents": ["r2", "rX"],   # 必须等于当前 tips，否则 409
    "species": "Cervus elaphus", "count": 5
  }'
```

## 样例脚本

启动 Compose 后直接执行（默认打 8001/8002，可用 `API_A`/`API_B` 覆盖）：

```bash
bash samples/01_out_of_order.sh              # 乱序：子先父后 → 待补齐 → 自动重算
bash samples/02_edit_delete_conflict.sh      # 改删并发 → 冲突 → 裁决 → 迟到分支 → 409
bash samples/03_idempotency_and_rejection.sh # 幂等重传；异内容/循环/跨观测整包拒绝
bash samples/04_assessment.sh                # 判读：确定超标/未超/待裁决见证/不给判定
```

## 一致性与并发设计

- 全部状态（修订、父子边、观测结论）都在同一个 SQLite 库文件中，两进程共享；
  重算在写入事务内同步完成，因此两端查询的分支与结论必然一致。
- 判读使用只读快照事务（WAL 下一读即固定快照），观测状态与分支载荷同源，
  并发汇交期间也不会拼出跨状态的结论。
- SQLite **WAL** 模式：读写互不阻塞；写事务一律 `BEGIN IMMEDIATE`，
  跨进程串行化写入，`busy_timeout` + 重试消除瞬时锁冲突。
- 库文件在命名卷上，进程重启、容器重建均保留。

## 本地开发与测试

```bash
pip install -r requirements.txt
pip install pytest httpx
python -m pytest tests/ -q          # 39 个行为测试
DB_PATH=./data/dev.db uvicorn app.main:app --port 8000
```
