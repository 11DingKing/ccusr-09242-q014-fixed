# 微专业就业成效追踪系统

维护毕业生、学院、微专业、就业去向、企业跟进和预警记录，支持按届次与组织维度追踪就业成效并保留分析依据。

## 运行约定

服务端代码位于 `app` 目录，默认使用项目目录中的 SQLite 文件。配置通过环境变量提供，导入演示数据前请确认数据库位置可写。

## 核验队列

`POST /api/v1/verification-queue/enqueue` 把待核验的毕业去向放入队列，可按风险等级、毕业届次和材料完整度登记；未指定风险时按去向状态与材料完整度自动评估。

- `POST /verification-queue/claim` 按优先级领取任务并签发短租约（默认 300 秒，可限幅 30-3600 秒），支持按届次/风险/材料完整度过滤；同一任务同一时间只会被一位老师领到，租约到期后自动回到可分配池。
- `POST /verification-queue/tasks/{id}/renew` 当前负责人续租。
- `POST /verification-queue/tasks/{id}/transfer` 当前负责人转交给其他老师并重置租约。
- `POST /verification-queue/tasks/{id}/return-supplement` 退回补件，任务退出领取池；`POST /verification-queue/tasks/{id}/supplement-received` 登记补件到位后重新入池；`POST /verification-queue/supplement-received/batch` 批量登记补件（可指定任务列表或按届次处理全部待补件任务，单条失败不影响其他记录）。
- `POST /verification-queue/tasks/{id}/complete` 当前负责人在有效租约内完成核验；租约到期或任务已重新分配后，迟到的完成请求会被拒绝，不会覆盖新负责人。
- `GET /verification-queue/tasks` 按有效优先级列出任务；`GET /verification-queue/tasks/{id}/events` 查看完整操作审计（每个动作都记录操作者与原因）。

优先级 = 风险基础分（高风险 7200）+ 等待老化分（每秒 1 分）：高风险记录优先被领取，普通记录等待足够久后必然反超，不会被饿死。所有状态变更基于版本号条件更新，并发安全；队列状态与虚拟时钟（`GET/POST /api/v1/clock`，`POST /api/v1/clock/advance` 推进时间）都持久化在数据库中，服务重启后自动恢复。

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
