# 排放核查台

汇总在线监测读数与治污设施工况，对排放读数进行核查登记，而不是把每次瞬时读数直接当成超标结论。

## 核查规则

- 登记字段：排放口、采样时刻、瞬时浓度、日均浓度、瞬时/日均许可限值、治污设施状态（运行中/停机）、最近校准时刻。
- **同一排放口同一采样时刻只保留首条登记**（数据库唯一约束，重复登记返回 409）。
- 瞬时浓度与日均浓度分别对照各自限值判定，瞬时波动与日均超标不混为一谈。
- 出现以下任一情形，登记后进入**待复核**，不由读数自动定性：
  - 治污设施停机期间数据；
  - 校准过期（采样时刻距最近校准超过 30 天）或无校准记录；
  - 瞬时浓度超瞬时限值，或日均浓度超日均限值。
- 待复核记录由**另一名合规人员**确认（四眼原则：复核人不能是登记人，也不能是最近一次更正记录的人）。
- 复核确认为重大事件（浓度达限值 3 倍及以上）的，必须在复核合格后有**复测达标**记录，且**整改事项全部关闭**，主管才能结案；复核排除（如瞬时波动）的不受该门槛限制。
- 限值或工况记录更正后，原复核与结案资格**立即失效**，状态回到待复核，旧复核/结案结论写入作废留档；旧复测在重新复核前不再作为结案依据。

## 模块结构（规则、存储、页面三块业务代码）

- `app.py`：参数解析、依赖组装和 HTTP 服务启动。
- `src/domain.py`：数据结构、错误、字段/时刻校验。
- `src/rules.py`：判定纯函数——超标分开计算、停机/校准阻断、状态机、角色矩阵、四眼与结案不变量。
- `src/repository.py`：SQLite 建表、唯一约束、乐观版本、结论留档、审计链。
- `src/service.py`：权限检查、用例编排、更正失效编排与审计。
- `src/http_api.py`：JSON 路由和统一错误响应。
- `static/index.html`：排放核查台页面，列表显示当前状态与阻断原因。
- `tests/`：规则、完整流程与失败/权限测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8313
```

默认端口 `8313`，首次启动自动建库。用 `X-Actor`、`X-Role` 请求头传递身份。角色：`operator`、`compliance_officer`、`director`、`viewer`。

## 接口

- `GET /health`
- `GET /api/checks?status=` 列表（含 `status`、`severity`、`blocker_reasons`、`current_blocker_reasons`）
- `POST /api/checks` 登记
- `GET /api/checks/{id}` 详情
- `POST /api/checks/{id}/correction` 更正限值/工况（须带 `expected_version`）
- `POST /api/checks/{id}/review` 复核（`conclusion`: `confirmed`/`rejected`）
- `POST /api/checks/{id}/close` 结案（重大事件校验复测与整改）
- `POST /api/checks/{id}/records` 追加复测（`kind=retest`，带 `result`、`sampled_at`）、整改（`kind=rectification`）、佐证
- `POST /api/checks/{id}/records/{rid}/close` 关闭整改事项
- `GET /api/checks/{id}/records` 复测/整改记录
- `GET /api/checks/{id}/history` 作废结论留档
- `GET /api/audit?check_id=` 审计链（director/viewer）

所有写操作返回当前 `version`，后续写操作须在 `expected_version` 中回传，并发修改返回 409。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
