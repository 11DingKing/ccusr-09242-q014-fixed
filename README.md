# 微专业就业成效追踪系统

维护毕业生、学院、微专业、就业去向、企业跟进和预警记录，支持按届次与组织维度追踪就业成效并保留分析依据。

## 运行约定

服务端代码位于 `app` 目录，默认使用项目目录中的 SQLite 文件。配置通过环境变量提供，导入演示数据前请确认数据库位置可写。

## 测试

在项目根目录执行：

```bash
python3 -m unittest discover -s tests -v
```

## 编译检查

在项目根目录执行：

```bash
python3 -m compileall -q app tests
```

## 启动服务

准备依赖后可执行 `uvicorn main:app --host 127.0.0.1 --port 8000`，根路径与 `/health` 返回服务状态，接口文档位于 `/docs`。

## 毕业去向核验队列

待核验去向通过 `/api/v1/verification` 下的接口分配，支持可恢复的领取租约：

| 动作 | 接口 |
| --- | --- |
| 入队 | `POST /verification/items`（风险等级、毕业届次、材料完整度） |
| 领取 | `POST /verification/claim`（按优先级领取下一条，或带 `item_id` 领取指定项，返回租约 `lease_token`） |
| 续租 | `POST /verification/items/{id}/renew` |
| 转交 | `POST /verification/items/{id}/transfer`（旧令牌立即失效） |
| 退回补件 | `POST /verification/items/{id}/return` |
| 批量补件 | `POST /verification/batch-resubmit`（补件到齐后重新入队并标记材料齐全，幂等） |
| 完成 | `POST /verification/items/{id}/complete` |
| 审计流水 | `GET /verification/items/{id}/events`、`GET /verification/events/list` |

语义约定：

* **防重入**：领取是带条件的原子更新（待领取或租约已到期才能成功），并发领取同一条记录只有一人成功。
* **租约与防迟到**：领取返回短暂租约（`owner` + `lease_token` + `lease_until`）。到期后记录可被他人重新分配；旧负责人用旧令牌的完成/退回/转交请求一律被拒绝，不能覆盖新负责人。
* **优先级与防饿死**：高风险相当于提前 600 秒入队，始终优先；普通记录等待时间持续老化，超过 600 秒后可反超新入队的高风险记录。同优先级下按毕业届次更早、材料更齐全、入队更早排序。
* **可恢复**：状态与审计流水持久化在 `verification_items` / `verification_events` 表，服务重启后租约、补件状态与操作记录均保留。
* **审计**：每个动作都记录操作者、原因、前后状态和发生时间。

### 虚拟时间（本地测试）

测试无需真实等待租约到期，可通过仅允许本机调用的接口推进进程内虚拟时钟：

* `GET  /api/v1/dev/clock` 查看当前虚拟时间
* `POST /api/v1/dev/clock/advance` `{"seconds": 600}` 向前推进
* `POST /api/v1/dev/clock/reset` 归零（服务重启同样归零）

队列相关测试：`python3 -m unittest tests.test_verification_queue -v`。
