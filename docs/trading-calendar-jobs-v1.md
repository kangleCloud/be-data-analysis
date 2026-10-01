# 交易日历缓存与内部手动任务 V1

## 共享交易日历

Redis DB 2 的 `stock:calendar:v1:trading-days` 是唯一的交易日历缓存，单键 JSON 写入且无 TTL：

```json
{
  "schemaVersion": 1,
  "source": "AKShare.tool_trade_date_hist_sina",
  "year": 2026,
  "refreshedAt": "2026-10-01T00:10:00+08:00",
  "firstDate": "2026-01-02",
  "lastDate": "2026-12-31",
  "dates": ["2026-01-02", "2026-01-05"]
}
```

AKShare 全量日期在写入前逐项严格验证，然后只保留 `year` 对应的上海时间当年交易日，并排序去重；示例数组仅展示开头，真实 `firstDate/lastDate` 必须与当年完整数组首尾一致。缓存不得包含其他年份日期。每年 1 月 1 日，旧年缓存对新年查询无效；当年无可用日期或查询日未被范围覆盖时为 `UNKNOWN`，不能凭工作日推断交易状态。刷新失败保留旧键，但旧年键不会被误用于新年。缓存覆盖当天时，源失败仍可沿用旧缓存；不足时市场快照按旧成功模块降级，个股采样跳过且不请求雪球。

服务启动时检查当天覆盖范围；正常每月 1 日上海时间 00:10 刷新一次。缺失、未覆盖或当月尚未刷新时按需补建，自动失败最多每天向新浪重试一次。刷新使用 Redis 锁；手动刷新另有 10 分钟最小间隔。AKShare 日历调用在限时子进程中运行；手动 HTTP 请求会等待本次任务的最终结果。

## 内部手动任务

所有接口均要求现有 `X-Internal-Token`。`kind` 只接受 `calendar`、`market`、`monitor`：

```http
POST /internal/jobs/v1/market/refresh
X-Internal-Token: <内部令牌>
```

请求同步等待业务任务完成，HTTP 200 返回最终结果：

```json
{"kind":"market","state":"PARTIAL","outcome":"partial","startedAt":"2026-10-01T10:00:00+08:00","finishedAt":"2026-10-01T10:01:00+08:00","message":"部分数据源失败，已按模块降级"}
```

终态为 `SUCCEEDED`、`PARTIAL`、`SKIPPED` 或 `FAILED`；`outcome` 记录 `published`、`partial`、`skipped`、`throttled`、`locked`、`cooldown`、`disabled`、`failed` 等具体业务结果。同类任务运行中再次触发不排队，立即返回 HTTP 409，响应体为相同结构且 `state=SKIPPED,outcome=locked`。日历限频、市场冷却等返回 HTTP 200、`SKIPPED` 及对应 `outcome`；源刷新失败返回 HTTP 200、`FAILED`。子进程、Redis 或运行超时等基础设施故障返回 HTTP 500 或 503、`FAILED`，不会伪装成成功。接口不提供任务号或状态查询。

市场任务在独立 Python 进程中执行，避免 AKShare 的 `SIGALRM` 超时逻辑在线程池运行。服务等待三个同花顺源的最坏超时和日历开销，上限 1260 秒；日历与个股任务分别限制为 60 秒和 300 秒。调用方的请求超时应高于 1260 秒。

三个手动任务与 CLI/自动调度调用同一业务流程。市场与个股手动触发不能绕过交易时段、日历缓存、分布式锁、120 秒请求间隔、源冷却或雪球总闸；日历可在任意时间手动刷新，但受独立锁和间隔限制。
