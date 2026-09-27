# 排放核查台

登记在线监测读数并完成排放核查：瞬时波动与日均超标分开判定，治污设施停机/仪器校准过期数据单独待复核，四眼复核、复测达标与整改关闭后方可结案；限值或工况更正后原结论立即失效并留档。

## 模块结构（规则 / 存储 / 页面三块分离）

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据校验、角色/状态常量与错误类型。
- `src/rules.py`：**规则块**——超标标记（瞬时/日均分开）、停机与校准过期判定、重大事件、复核分流、结案阻断。
- `src/repository.py`：**存储块**——SQLite建表、`(排放口, 采样时刻)`唯一约束、版本/修订号、旧结论归档表、SHA-256审计链。
- `src/service.py`：权限、用例编排、四眼复核、更正失效与留档、结案不变量。
- `src/http_api.py`：JSON路由和统一错误响应。
- `static/index.html`：**页面块**——登记表单与核查列表，显示当前状态和阻断原因。
- `tests/`：规则、完整流程与失败场景测试。

## 核查规则

- 登记字段：排放口、采样时刻、瞬时浓度、日均浓度、瞬时许可限值、日均许可限值、治污设施状态、仪器校准有效期。
- 同一排放口同一采样时刻仅保留首条读数，重复登记返回409。
- 以下任一情况读数进入**待复核**：治污设施停机、校准过期、瞬时浓度超瞬时限值、日均浓度超日均限值；其余为已登记。
- 待复核必须由**登记人之外的另一名合规人员**确认（结论：确认超标/瞬时波动/数据无效）。仅日均超标为**重大事件**，进入整改中；瞬时波动或无效数据复核后即成立。
- 重大事件结案条件：存在达标的复测读数（对照当前限值）且整改事项全部关闭。
- 限值或治污工况更正后：强制回到待复核，`revision`递增，原复核/结案字段清空、资格立即失效；旧结论以快照形式写入归档（`case_archives`）留档，且不能跳过复核直接结案。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8313
```

默认端口`8313`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。
角色：operator（运维登记/整改复测）、compliance_officer（复核/更正/结案）、director、viewer。

## 主要接口

- `GET /health`
- `GET /api/cases`（可带`?status=pending`）/ `POST /api/cases`
- `GET /api/cases/{id}`
- `POST /api/cases/{id}/review`：复核确认，须提交`expected_version`
- `POST /api/cases/{id}/correct`：限值/工况更正（旧结论失效留档）
- `POST /api/cases/{id}/records`：`kind=rectification|retest|note`
- `POST /api/cases/{id}/records/{rid}/close`：关闭整改事项
- `POST /api/cases/{id}/close`：重大事件结案
- `GET /api/cases/{id}/archives`：旧复核/结案留档
- `GET /api/cases/{id}/records` / `GET /api/audit`

列表与详情中的`blockers`字段即当前阻断原因（待复核原因、缺达标复测、未关闭整改数等）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
