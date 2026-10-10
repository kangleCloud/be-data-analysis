# 核心指数与 ETF 数据源 V1

## 源接口与边界

| 用途 | AKShare 入口 | 源域名 | 结果与限制 |
| --- | --- | --- | --- |
| 五只核心指数 | `stock_zh_index_spot_sina` | `vip.stock.finance.sina.com.cn` | 最新点位、涨跌、成交；无可靠逐条源时间 |
| ETF 字典和交易价格 | `fund_etf_category_sina(symbol="ETF基金")` | `vip.stock.finance.sina.com.cn` | `最新价` 是交易价格；无可靠源时间；不提供可信的 ETF 资金流 |
| ETF 基本资料 | `fund_info_ths(symbol=六位代码)` | `fund.10jqka.com.cn` | 基金全称、类型、投资类型、基金经理、成立日期、业绩比较基准、管理人和托管人；缺字段可空 |
| 雪球 ETF 资产配置 | `fund_individual_detail_hold_xq` | `danjuanfunds.com` | 仅资产类型仓位比例，不是成份股；总闸默认关闭 |

固定 AKShare 1.18.97。本地源码审计确认 `fund_info_ths` 使用 `requests.get` 请求 `https://fund.10jqka.com.cn/{symbol}/interduce.html`，返回“字段／值”两列表。Python 调用 AKShare，未修改安装包或另写网页解析器。Provider 只放行已审计域名，资料请求禁止跟随重定向，设置 HTTP 超时并在隔离子进程执行。ETF 基本资料只使用同花顺入口，不调用交易所份额、雪球 basic 或东方财富接口。源码审计与模拟验证不代表所有 ETF 在源站均有可用资料。

核心指数固定为上证指数 `sh000001`、深证成指 `sz399001`、沪深300 `sh000300`、创业板指 `sz399006`、科创50 `sh000688`。五条缺任意一条时整个 `coreIndices` 模块降级，不混合不完整的新批次与旧批次。指数数据包含 `sourceTime:null`；`tradeDateBasis:CALENDAR` 表示交易日期来自日历，`collectedAt` 只表示 Python 获取时间。

## ETF Redis 契约

Java 将已启用列表写入 `stock:etf-monitor:v1:enabled`，值为按显示顺序排列、最多 10 个对象的 JSON 数组，例如 `[ {"symbol":"SH510050","code":"510050","name":"50ETF","market":"SH"} ]`。代码使用 `SH`/`SZ` 加六位数字。Python 每个交易日采集窗口至少间隔 120 秒读取列表，一次获取新浪 ETF 全表，逐只写入独立快照。

- 快照键 `stock:etf-monitor:v1:snapshot`；状态 ID 键 `stock:etf-monitor:v1:state-id`。
- 更新频道 `stock:etf-monitor:v1:updates`；消息为 `{ "baseStateId":null, "stateId":"...", "changedSymbols":["SH510050"] }`。快照写入、状态 ID 与通知使用同一 Redis 事务。
- 价格曲线键 `stock:etf-monitor:v1:price-series:YYYY-MM-DD:SYMBOL`，保留最近两个有采样数据的交易日期。无源时间时，曲线点的 `collectedAt` 仅为采集时间。
- `items` 包含 `symbol/code/name/market`、`quote`、`priceSeries`、`fundSeries`。`quote.status` 为 `FRESH` 或 `STALE`；失败保留上次有效价格并标记 `STALE`，无历史数据时 `quote:null`。`quote.sourceTime:null`，公开视图的 `closeConfirmed:false`；不可根据 15:00 后的采集时间推断收盘确认。
- `fundSeries:[]`、`fundFlowStatus:"NO_RELIABLE_SOURCE"` 表示没有可靠的 ETF 资金流数据。不能以份额或基金净值伪造资金流及交易价格。

## 内部同步接口

所有接口使用 `X-Internal-Token`。`POST /internal/etf-monitor/v1/dictionary` 返回新浪 ETF 字典。`POST /internal/etf-monitor/v1/profiles` 只接收 `{ "symbols":["SH510050"] }`，最多 10 个不重复代码；旧 `asOfDate` 字段返回 422。资料不受雪球总闸控制，不加入 120 秒行情采集。

资料响应示例（模拟）：

```json
{
  "schemaVersion": 1, "source": "THS", "collectedAt": "2026-10-03T10:00:05+08:00",
  "profiles": [{
    "symbol": "SH510050", "code": "510050", "source": "THS",
    "collectedAt": "2026-10-03T10:00:03+08:00",
    "fullName": "上证50交易型开放式指数基金", "fundType": "股票型",
    "investmentType": "指数型", "fundManager": null,
    "establishedDate": "2004-12-30", "performanceBenchmark": null,
    "manager": null, "custodian": null
  }],
  "sourceStatus": {"SH510050":"OK", "SZ159919":"ERROR", "SH588000":"SKIPPED"}
}
```

源表基金代码必须与请求六位代码一致；冲突、空表、字段结构变化或全部资料字段为空时，该 symbol 为 `ERROR`。每个 profile 的 `collectedAt` 是该次获取及标准化时间；Java 使用它作为公开 `updatedAt`。顶层 `collectedAt` 是批次完成时间。成立日期仅为 `establishedDate`，基金经理仅为 `fundManager`，基金管理人是 `manager`；业绩比较基准不推断跟踪指数。资料不再提供 `listingStatus/listingDate/shareCount/shareDate`，Java 成功切换时清空旧字段，失败时保留原资料。

Python 按 symbol 对资料请求限制每 30 分钟一次（失败尝试也计入），串行源请求至少间隔 2 秒，批次总预算 180 秒。未尝试的预算／限频项目标记 `SKIPPED`；部分有效返回 HTTP 200 和逐 symbol 状态。全部无有效资料返回 502，全部限频跳过返回 429，批次锁冲突返回 409，基础设施不可用返回 503，不能以空列表覆盖旧资料。

Python 自己持有 `stock:etf-monitor:v1:profiles:python:lock`（210 秒）和 `stock:etf-monitor:v1:profiles:python:min-interval:SYMBOL`（1800 秒），只用于资料批次。Java 复用整体刷新锁；Python 不获取或释放 Java 的锁，防止 Java 持锁调用时自锁。行情快照、键和事件不变。Java 同步等待预算：dictionary 60 秒、profiles 210 秒、allocation 120 秒；前端整体请求 300 秒。

`POST /internal/etf-monitor/v1/asset-allocation` 接收 `{ "symbol":"SH510050", "reportPeriod":"20260630" }`，仅在 `STOCK_MONITOR_XQ_ENABLED=true` 且令牌已配置时查询，返回资产类型和仓位百分比。

任务入口为`POST /internal/jobs/v1/etf/refresh`，先验证内部Token，再解析X-Collection-Mode（缺省auto，auto/manual外400）。auto及CLI `python -m app etf-collect`在交易窗口内遵守日历、采集锁、120秒间隔与普通冷却，窗口外跳过。manual可休市或窗口外刷新，跳过采集锁/入口/源配额、任务间隔及普通冷却；HTTP共享限速、401/403/429及确认风控、资源与事务写入保护仍生效。无可靠源日期时tradeDate=null，只更新快照/实际collectedAt，不生成价格/资金日内点，不以工作日推测交易日。ETF字典auto复用合法当日缓存，manual强制一次新浪全表，失败保留旧缓存。
