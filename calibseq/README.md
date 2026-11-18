# calibseq · 校准分组序贯判定

反复加测会把偶然达标的批次放行（多重比较放大误放率）。本系统把校准复核组织为
**分组序贯检验**：开案时一次定好各温区容差、每轮累计样本量与整案误放风险上限，
之后按轮补报、齐量判定，各轮各温区共用同一整案风险预算。

仅依赖 Python 3.9+ 标准库（`sqlite3` + `http.server`）与无构建原生前端，离线可运行。

## 运行

```bash
cd /workspace
python3 -m calibseq.server --host 127.0.0.1 --port 8000 --db calibseq.db
# 打开 http://127.0.0.1:8000  （页面：开案 / 补报 / 判定 / 恢复 / 每轮区间与风险分配核对）
```

## 测试

```bash
cd /workspace
python3 -m unittest discover -s tests -v
```

## 需求映射

| 需求 | 实现 |
|---|---|
| 开案定容差、每轮累计样本量、整案误放风险上限 | `POST /api/cases`：`zones[].lower/upper`、`rounds[]`（严格递增累计量）、`overall_risk` |
| 各轮各温区共用整案风险上限 | Bonferroni 均分：α<sub>轮</sub>=α/R，α<sub>轮,区</sub>=α/(R·Z)；并集界保证总误放概率 ≤ α |
| 按轮补报、编号不重复 | 测量记录 `(case_id, record_id)` 主键；同编号同内容幂等 `duplicate`，同编号不同内容 `conflict` |
| 所有温区齐量才判定 | 判定前校验每温区累计数 ≥ 本轮配额，否则返回 `insufficient` 与缺口 |
| 区间全落容差内放行，否则加测，末轮转人工 | 每温区 (1−α<sub>轮,区</sub>) 双侧 t 区间；`PASS`→放行，`CONTINUE`→下一轮，末轮未过→`MANUAL_REVIEW` |
| 并发补报与判定按同一份封存读数裁决 | 判定先在 `BEGIN IMMEDIATE` 事务内封存快照（`SEALED`）再计算；封存后到达的补报不计入本次 |
| 定案后拒绝补报 | 状态机 `OPEN→RELEASED/MANUAL_REVIEW`；补报返回 `409 CASE_DECIDED` |
| 计算中断按封存读数恢复 | 封存快照持久化于 `judgment_runs.snapshot`；`POST /api/cases/{id}/resume` 或服务启动时自动完成 |
| 重试不新增风险额度占用 | 风险分配是 (开案配置, 轮次) 的纯函数；重试续算同一封存运行（幂等回放），不新建运行 |

## 统计方法

- 第 r 轮温区 z 的判定区间：截至封存的全部累计读数，均值的
  (1 − α/(R·Z)) 双侧 t 置信区间（`calibseq/stats.py`，纯标准库实现，
  经已知分位数表校验）。
- 误放控制：若温区 z 真值在容差外，则第 r 轮误判放行蕴含该区间漏掉真值，
  其概率 ≤ α/(R·Z)；对全部 R×Z 个 (轮, 温区) 取并集界，整案误放概率 ≤ α。
- 区间全落容差内 ⇒ `PASS`；任一温区未落入且未达末轮 ⇒ `CONTINUE`（加测）；
  末轮仍未齐 ⇒ `MANUAL_REVIEW`（转人工复校）。

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| GET | `/api/cases` | 案件列表 |
| POST | `/api/cases` | 开案 `{name, overall_risk, zones:[{zone_id?,name?,lower,upper}], rounds:[n1,n2,...]}` |
| GET | `/api/cases/{id}` | 详情：状态、风险分配、齐量进度、测量记录、判定历史 |
| POST | `/api/cases/{id}/measurements` | 批量补报 `{records:[{record_id?,zone_id,value}]}`，逐条返回 `stored/duplicate/conflict/invalid` |
| POST | `/api/cases/{id}/judge` | 判定当前轮：`insufficient / completed(PASS·CONTINUE·MANUAL_REVIEW) / decided`；已有封存则续算 |
| POST | `/api/cases/{id}/resume` | 恢复被中断的封存判定（服务启动时亦自动执行） |

错误统一为 `{"error": {"code", "message"}}`：`VALIDATION`(400)、`NOT_FOUND`(404)、
`CASE_DECIDED`(409)、`CONFLICT`(409)。

## 目录

```
calibseq/
  stats.py            # t 分布分位数、均值置信区间（纯标准库）
  store.py            # SQLite 模式、连接、BEGIN IMMEDIATE 事务、每连接锁
  core.py             # 开案/补报/封存/定案/恢复 业务逻辑
  server.py           # HTTP API 与静态页面宿主
  static/index.html   # 单页：开案、补报、判定、恢复、每轮区间与风险分配核对
tests/                # 统计精度、业务规则、并发与恢复、API 冒烟（31 例）
```
