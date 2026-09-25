# 器官分配与转运协调系统

Python 标准库独立项目。系统按器官类型、血型、地域、医疗匹配、紧急程度和等待时间排序候选患者，并管理提出、接受、转运、交接、植入或撤回流程。器官过期后所有继续流转操作都会被阻止，全部状态变化写入审计记录。

## 运行

```bash
python3 app.py --db organ_allocation.db
```

默认监听 `127.0.0.1:8203`，首页 `/`，健康检查 `/health`。

身份头：`X-User-Id`、`X-Role`。角色为 `viewer`、`hospital`、`coordinator`、`allocation_officer`、`auditor`；医院角色还需 `X-Hospital`。

## 主要接口

- `POST /api/donors`、`POST /api/candidates`：登记器官与候选患者。
- `GET /api/donors/{id}/ranking`：查看兼容候选排序。
- `POST /api/allocations`：提出唯一分配。
- `POST /api/allocations/{id}/accept`、`withdraw`：医院确认或撤回。
- `POST /api/allocations/{id}/transit`、`delay`：冷链转运和延误上报。
- `POST /api/allocations/{id}/eta`：医院上报预计抵达时间。
- `GET /api/allocations/{id}/reassign-options`：调度按剩余可用分钟筛出赶得上的候选患者。
- `POST /api/allocations/{id}/reassign`：分配员转运前改派，必须保留改派原因。
- `POST /api/allocations/{id}/handoff`、`handoff-accept`：来源医院发起、接收医院确认。
- `POST /api/allocations/{id}/implant`：确认植入。
- `GET /api/allocations/{id}/audit`、`GET /api/state`：完整审计和权限视图。

## 转运前改派

候选患者登记时可带 `transit_minutes`(预计转运分钟，默认 60)。剩余可用分钟 = 器官到期时间 − 当前时间，只有 `transit_minutes` 不超过剩余分钟的患者才算赶得上。医院上报预计抵达后若原患者来不及接收可撤回；分配员从名单中改派并填写原因，分配回到“待接受”且原预计抵达作废，需新医院重新确认。改派后旧医院再用原分配编号确认会收到 `allocation_reassigned`(409)提示内容已经变化；转运开始后不能改派。页面将规则、记录与办理入口分区维护。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

血型兼容与评分是演示规则，不包含 HLA 分型、器官大小、病程、儿科差异和真实移植网络规则。`transit_minutes` 是人工维护的估计值，不接入实时路况。医院身份使用请求头模拟，SQLite 环境适合原型，不处理跨机构身份信任、远程患者隐私协议和真实冷链设备接入。
