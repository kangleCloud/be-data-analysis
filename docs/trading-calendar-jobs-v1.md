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

服务启动时检查当天覆盖范围；正常每月 1 日上海时间 00:10 刷新一次。缺失、未覆盖或当月尚未刷新时按需补建，自动失败最多每天向新浪重试一次。AUTO刷新使用Redis采集锁，内部按需刷新另有10分钟间隔；显式manual跳过这些准入限制，两模式仍有短缓存写锁、共享HTTP间隔和风控。AKShare 日历调用在限时子进程中运行；手动 HTTP 请求会等待本次任务的最终结果。

## 内部手动任务

Python 内部接口均要求现有 `X-Internal-Token`。`kind` 只接受 `calendar`、`market`、`monitor`、`etf`；该内部路由供 Java 调用，与本机 scheduler 的固定业务路径不同：

```http
POST /internal/jobs/v1/market/refresh
X-Internal-Token: <内部令牌>
X-Collection-Mode: manual
```

请求同步等待业务任务完成，HTTP 200 返回最终结果：

```json
{"kind":"market","state":"PARTIAL","outcome":"partial","startedAt":"2026-10-01T10:00:00+08:00","finishedAt":"2026-10-01T10:01:00+08:00","message":"部分数据源失败，已按模块降级"}
```

终态为 `SUCCEEDED`、`PARTIAL`、`SKIPPED` 或 `FAILED`；`outcome` 记录 `published`、`partial`、`skipped`、`throttled`、`locked`、`cooldown`、`disabled`、`failed` 等具体业务结果。AUTO同类任务运行中再次触发不排队，立即返回 HTTP 409，响应体为相同结构且 `state=SKIPPED,outcome=locked`。AUTO日历限频与ETF/个股源保护跳过沿原HTTP 200、`SKIPPED`及对应outcome；市场受保护模块按原逻辑降级为PARTIAL；源刷新失败返回 HTTP 200、`FAILED`。子进程、Redis 或运行超时等基础设施故障返回 HTTP 500 或 503、`FAILED`，不会伪装成成功。接口不提供任务号或状态查询。

业务编排与标准化在服务父进程执行，每次AKShare源调用由独立spawn子进程执行和回收，避免在线程池内运行AKShare超时逻辑。服务等待同花顺和新浪指数源及日历开销，上限 1380 秒；日历源预算为SOURCE_TIMEOUT_SECONDS+12（默认27秒，含回收）、个股行情与ETF行情批次分别300、120秒。调用方的请求超时应高于对应上限，市场请求建议至少等待 1440 秒。

四种jobs任务与CLI/自动调度复用同一业务流程。先认证X-Internal-Token再校验X-Collection-Mode；缺省auto，仅精确auto/manual有效，其他400。auto保留交易窗口、日历、入口/任务锁、源配额、间隔和冷却。manual跳过普通失败冷却、采集锁/入口/源配额和任务/单股/资料/日历间隔，可休市或窗口外执行，每次请求仍串行；HTTP共享限速、401/403/429及明确风控、未知旧保护、雪球总闸/Token、≤10、截止与资源保护以及Redis短写锁/事务仍生效。无可靠源日期的市场/指数/ETF窗口外只更新快照与实际采集时间，tradeDate=null，不新增日内点；股票保留真实源日期与时间且不能倒退，严格>15:00收盘，不补历史点。详细契约见[source-health-check.md](source-health-check.md)。

## 本机 scheduler 与 Python 业务映射

Java 本机入口前缀为 `POST /scheduler/api/local/market-data/v1`，下挂八个固定路径；HTTP 不通过 `kind` 参数选择任务。本机入口仅接受真实回环直连并拒绝转发头，无需登录或令牌。Java 调用以下 Python 内部入口时仍须携带 `X-Internal-Token`，并明确发送`X-Collection-Mode: manual`；admin/定时保持auto。本机访问控制由 Java 实现，不能据此省略 Python 的认证。

| scheduler 后缀 | Python POST 路径 | Python 方法与同步结果 |
| --- | --- | --- |
| `/stock/dictionary/refresh` | `/internal/stock-monitor/v1/exchange-dictionary` | `ExchangeStockProvider.all_a_stocks()`；`{schemaVersion,stocks}` |
| `/stock/profiles/refresh` | `/internal/stock-monitor/v1/profiles` | `XueqiuProvider.profile()`，必要时 `quote()` 补市值；`{schemaVersion,profiles}` |
| `/calendar/refresh` | `/internal/jobs/v1/calendar/refresh` | `run_calendar(on_demand=True, mode="manual")`；任务终态包；源默认27秒 |
| `/market/refresh` | `/internal/jobs/v1/market/refresh` | `run_market(mode="manual")`；任务终态包；1380 秒 |
| `/stock/quotes/refresh` | `/internal/jobs/v1/monitor/refresh` | `run_monitor(mode="manual")`；任务终态包；300 秒 |
| `/etf/dictionary/refresh` | `/internal/etf-monitor/v1/dictionary` | `AkShareEtfProvider.quotes()` → `catalog()`；`{schemaVersion,source,collectedAt,etfs}` |
| `/etf/profiles/refresh` | `/internal/etf-monitor/v1/profiles` | `fund_info_ths(六位代码)` → `ths_profile()`；`{schemaVersion,source:THS,sourceStatus,collectedAt,profiles}` |
| `/etf/quotes/refresh` | `/internal/jobs/v1/etf/refresh` | `run_etf(mode="manual")`；任务终态包；120 秒 |

字典／资料接口同步返回数据包，由 Java 业务服务完成持久化并返回业务结果；它们不返回 `kind/state/outcome`。四个任务接口返回前述终态包，不创建 `jobId`。资料请求只传 `symbols`，最多 10 个。ETF 同花顺资料不受雪球总闸控制。ETF 资产配置是独立内部接口 `/internal/etf-monitor/v1/asset-allocation`，不属于这八个固定入口；只有雪球总闸开启且令牌已配置时才可调用。

交易所股票字典每个 HTTP 请求使用 `SOURCE_TIMEOUT_SECONDS`；股票资料最多逐只调用 profile 和 quote，调用间隔至少一秒。ETF 同花顺资料批次总预算 180 秒、逐 symbol 30 分钟限频、源请求至少间隔 2 秒，Java 等待 210 秒；ETF 字典及资产配置等待预算分别为 60、120 秒。Java 的数据同步等待时间须覆盖整个批次，不能套用单次 HTTP 请求超时。资料批次锁由 Python 独立持有，Java 整体刷新锁归 Java，两者不竞争同一个键。

字典和同花顺 ETF 资料同步不写行情快照、曲线或发布行情事件；普通大屏 GET 刷新仅读快照，实时更新继续使用 SSE 和自动重同步。管理端与定时整体刷新仍可复用这些业务方法。
