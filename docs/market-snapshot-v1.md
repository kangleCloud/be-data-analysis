# 市场快照 V1 契约

`be-data-analysis` 向 Redis DB 2 的 `stock:market:v1:snapshot` 写入普通 UTF-8 JSON。`be-vita` 应按字符串读取并解析 JSON，不使用带 Java 类型信息的 Redis 对象反序列化器。单键 `SET` 保证读到完整一轮快照。

## 顶层与模块

```json
{
  "schemaVersion": 1,
  "provider": "akshare",
  "generatedAt": "2026-09-23T10:00:00+08:00",
  "modules": {
    "industryHeatmap": {},
    "conceptHeatmap": {},
    "industryTop5": {},
    "conceptTop5": {},
    "marketFundFlow": {}
  }
}
```

每个模块含 `status`、`tradeDate`、`tradeDateBasis`、`lastSuccessAt`、`lastAttemptAt`、`message`、`data`。`status` 为 `FRESH`、`STALE` 或 `ERROR`：成功采集为 `FRESH`；失败且有旧数据为 `STALE`；失败且没有旧数据为 `ERROR`，此时 `data=null`。`FRESH` 表示本轮**读取成功**，并不保证源站发布了当日数据，应同时检查 `tradeDate`。

板块列表和板块资金流源接口未提供可靠交易日期，`tradeDateBasis=CALENDAR` 表示日期来自采集时的交易日历，`lastSuccessAt` 是采集完成时间而非源站报价时间。大盘资金流使用源数据的 `日期`，`tradeDateBasis=SOURCE`；若盘中最新一行仍是前一交易日，就如实保留该日期。

## data 字段

| 模块 | 内容 |
| --- | --- |
| `industryHeatmap`、`conceptHeatmap` | 板块数组：`sectorCode`、`sectorName`、`sectorType`、`marketCap`、`changePercent`、`turnoverRate`、`riseCount`、`fallCount`、`leadingStockName`。 |
| `industryTop5`、`conceptTop5` | `topRise`、`topFall`、`topInflow`、`topOutflow` 四个数组，各最多五项；另有 `unmatchedFundRows`。资金榜项有 `mainNetInflow`、`mainNetInflowRatio`。 |
| `marketFundFlow` | `latest` 为最新可得交易日记录；`series` 为按日期升序排列的最多 20 条交易日记录。含主力、超大单、大单、中单、小单净流入及净占比，以及上证、深证收盘点位和涨跌幅。 |

金额及总市值单位为**元**；涨跌幅、换手率及净流入占比是百分数数值，例如 `2.5` 表示 `2.5%`，不是 `0.025`。无法确定的数值为 JSON `null`，不得写入 `NaN`、`Infinity` 或零值替代。资金流遵循 AKShare 对东方财富数据的字段口径。

来源：[AKShare 股票数据文档](https://akshare.akfamily.xyz/data/stock/stock.html)。
