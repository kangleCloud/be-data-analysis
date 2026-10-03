# 核心指数与 ETF 数据源 V1

## 源接口与边界

| 用途 | AKShare 入口 | 源域名 | 结果与限制 |
| --- | --- | --- | --- |
| 五只核心指数 | `stock_zh_index_spot_sina` | `vip.stock.finance.sina.com.cn` | 最新点位、涨跌、成交；无可靠逐条源时间 |
| ETF 字典和交易价格 | `fund_etf_category_sina(symbol="ETF基金")` | `vip.stock.finance.sina.com.cn` | `最新价` 是交易价格；无可靠源时间；不提供可信的 ETF 资金流 |
| 上交所 ETF 资料 | `fund_etf_scale_sse(date=YYYYMMDD)` | `query.sse.com.cn` | 基金份额由 AKShare 换算为份，源有统计日期 |
| 深交所 ETF 资料 | `fund_etf_scale_szse()` | `fund.szse.cn` | 原字段“当前规模(份)”转换为基金份额；源列表无统计日期 |
| 雪球 ETF 资产配置 | `fund_individual_detail_hold_xq` | `danjuanfunds.com` | 仅资产类型仓位比例，不是成份股；总闸默认关闭 |

这五个入口的源代码与受控实测均未调用东方财富。Provider 对请求域名设白名单、超时与请求间隔。AKShare 1.18.97 的深交所接口将 XLSX 原始 bytes 传给 `pandas.read_excel`，当前 pandas 拒绝该参数；隔离子进程内将 bytes 包装成 `BytesIO`，实测返回 1062 行。上交所 2026-09-30 份额接口实测返回 920 行。接口字段和数量会随源站变化，不能视作固定总数。

核心指数固定为上证指数 `sh000001`、深证成指 `sz399001`、沪深300 `sh000300`、创业板指 `sz399006`、科创50 `sh000688`。五条缺任意一条时整个 `coreIndices` 模块降级，不混合不完整的新批次与旧批次。指数数据包含 `sourceTime:null`；`tradeDateBasis:CALENDAR` 表示交易日期来自日历，`collectedAt` 只表示 Python 获取时间。

## ETF Redis 契约

Java 将已启用列表写入 `stock:etf-monitor:v1:enabled`，值为按显示顺序排列、最多 10 个对象的 JSON 数组，例如 `[ {"symbol":"SH510050","code":"510050","name":"50ETF","market":"SH"} ]`。代码使用 `SH`/`SZ` 加六位数字。Python 每个交易日采集窗口至少间隔 120 秒读取列表，一次获取新浪 ETF 全表，逐只写入独立快照。

- 快照键 `stock:etf-monitor:v1:snapshot`；状态 ID 键 `stock:etf-monitor:v1:state-id`。
- 更新频道 `stock:etf-monitor:v1:updates`；消息为 `{ "baseStateId":null, "stateId":"...", "changedSymbols":["SH510050"] }`。快照写入、状态 ID 与通知使用同一 Redis 事务。
- 价格曲线键 `stock:etf-monitor:v1:price-series:YYYY-MM-DD:SYMBOL`，保留最近两个有采样数据的交易日期。无源时间时，曲线点的 `collectedAt` 仅为采集时间。
- `items` 包含 `symbol/code/name/market`、`quote`、`priceSeries`、`fundSeries`。`quote.status` 为 `FRESH` 或 `STALE`；失败保留上次有效价格并标记 `STALE`，无历史数据时 `quote:null`。`quote.sourceTime:null`，公开视图的 `closeConfirmed:false`；不可根据 15:00 后的采集时间推断收盘确认。
- `fundSeries:[]`、`fundFlowStatus:"NO_RELIABLE_SOURCE"` 表示没有可靠的 ETF 资金流数据。不能以份额或基金净值伪造资金流及交易价格。

## 内部同步接口

所有接口使用 `X-Internal-Token`。`POST /internal/etf-monitor/v1/dictionary` 返回 ETF 字典。`POST /internal/etf-monitor/v1/profiles` 接收 `{ "symbols":["SH510050"], "asOfDate":"20260930" }`，分别查询上交所和深交所，返回已查得资料及两个源的状态；深交所来源不保证统计日期。`POST /internal/etf-monitor/v1/asset-allocation` 接收 `{ "symbol":"SH510050", "reportPeriod":"20260630" }`，仅在 `STOCK_MONITOR_XQ_ENABLED=true` 且令牌已配置时查询，返回资产类型和仓位百分比。

手动任务为 `POST /internal/jobs/v1/etf/refresh`，在交易窗口内遵守交易日历、Redis 锁、120 秒间隔与源冷却。可直接运行 `python -m app etf-collect` 作同等采样。交易时间外返回跳过，不用工作日猜测交易日。
