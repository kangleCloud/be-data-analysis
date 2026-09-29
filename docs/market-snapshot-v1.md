# 市场快照 V1 契约

`be-data-analysis` 向 Redis DB 2 的 `stock:market:v1:snapshot` 写入普通 UTF-8 JSON 字符串，不设 TTL。单键 `SET` 成功后向 `stock:market:v1:updates` 发布包含同一快照 `schemaVersion` 和 `generatedAt` 的 JSON 通知。通知仅唤醒读取方，读取方重新读取完整快照。锁键为 `stock:market:v1:lock`。

## 顶层与模块

```json
{
  "schemaVersion": 1,
  "provider": "akshare",
  "generatedAt": "2026-09-25T10:10:00+08:00",
  "modules": {
    "industryTop5": {},
    "conceptTop5": {},
    "marketFundFlow": {}
  }
}
```

真实模块各含 `status`、`tradeDate`、`tradeDateBasis`、`lastSuccessAt`、`lastAttemptAt`、`message`、`data`。`FRESH` 表示本轮读取成功；失败且有旧成功数据为 `STALE`；失败且没有旧数据为 `ERROR`，此时 `data=null`。三个模块独立降级。`generatedAt` 是本轮快照生成时间，`lastSuccessAt` 是采集完成时间，不代表源站报价时间。

`industryTop5` 和 `conceptTop5` 分别来自 AKShare 的同花顺 `stock_fund_flow_industry(symbol="即时")` 和 `stock_fund_flow_concept(symbol="即时")`。这两个模块的 `tradeDateBasis=CALENDAR`，`tradeDate` 仅是采集时参考交易日，不声称同花顺源数据日期。`marketFundFlow` 使用 AKShare 的东方财富 `stock_market_fund_flow()`，`tradeDateBasis=SOURCE` 且交易日期取自源数据的 `日期`；`FRESH` 不保证源日期是当天。

## data 字段

| 模块 | 内容 |
| --- | --- |
| `industryTop5`、`conceptTop5` | 对象含 `source="THS"`、`period="INTRADAY"`、`topRise`、`topFall`、`topInflow`、`topOutflow`，四个榜单各最多五项。 |
| `marketFundFlow` | `latest` 为最新可得交易日记录；`series` 为按日期升序排列的最多 20 条交易日记录。保留东方财富主力、超大单、大单、中单、小单净流入及净占比，以及上证、深证收盘点位和涨跌幅。 |

Top5 四类榜单的每个条目均为：

```json
{
  "sectorName": "半导体",
  "sectorType": "industry",
  "changePercent": 2.5,
  "netFlowAmount": 150000000
}
```

`sectorType` 为 `industry` 或 `concept`。同花顺 `净额` 原单位为亿元，转换成以元为单位的 `netFlowAmount`；它不是东方财富的主力净流入。涨幅榜按正涨跌幅降序，跌幅榜按负涨跌幅升序；流入榜按正净额降序，流出榜按负净额升序，即流出绝对金额降序。同值按 `sectorName` 字符串升序。空名、重复名、无效数值不进入榜单；无可用行时按模块失败降级。

金额单位为元，百分数字段的 `2.5` 表示 `2.5%`。无法确定的数值为 JSON `null`，不得用 `NaN`、`Infinity` 或伪造零值代替。

## 采集时段和源保护

`python -m app serve` 启动内置调度。北京时间工作日 09:40、10:10、10:40、11:10、13:10、13:40、14:10、14:40、15:30 各启动一次独立 `python -m app collect` 子进程；服务启动时已错过的时段不补跑，节假日由交易日历检查后跳过实际模块采集。手动 `python -m app collect --force` 只绕过时段检查，不绕过 Redis 分布式时段占位、全局 20 分钟最小间隔或源冷却。被限流、断连或超时的源进入 Redis 共享两小时冷却期；同花顺两个榜单共用源冷却。源调用不立即重试，同花顺分页请求至少间隔 1 秒且单次接口超时 45 秒，其余接口默认超时 15 秒。

来源：[AKShare 股票数据文档](https://akshare.akfamily.xyz/data/stock/stock.html)。
