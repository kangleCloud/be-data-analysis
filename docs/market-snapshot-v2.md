# 市场快照 V2 契约

`be-data-analysis` 向 Redis DB 2 的 `stock:market:v2:snapshot` 写入普通 UTF-8 JSON 字符串，不设 TTL。单键 `SET` 成功后向 `stock:market:v2:updates` 发布一条包含 `schemaVersion` 和 `generatedAt` 的 JSON 通知。通知只用于唤醒读取方；读取方始终重新读取完整快照。V1 键不参与 V2 的读取和降级。

## 顶层与模块

```json
{
  "schemaVersion": 2,
  "provider": "akshare",
  "generatedAt": "2026-09-25T10:00:00+08:00",
  "modules": {
    "industryHeatmap": {},
    "conceptHeatmap": {},
    "industryTop5": {},
    "conceptTop5": {},
    "marketFundFlow": {}
  }
}
```

每个真实模块含 `status`、`tradeDate`、`tradeDateBasis`、`lastSuccessAt`、`lastAttemptAt`、`message` 和 `data`。`status` 为 `FRESH`、`STALE` 或 `ERROR`：本轮读取成功为 `FRESH`；失败但有 V2 旧成功数据为 `STALE`；失败且没有可用旧数据为 `ERROR`，此时 `data=null`。五个模块独立降级。顶层 `generatedAt` 是本轮快照生成时间。

行业与概念热力图使用东方财富板块行情。`industryTop5` 和 `conceptTop5` 分别使用 AKShare 的同花顺 `stock_fund_flow_industry(symbol="即时")`、`stock_fund_flow_concept(symbol="即时")`，不依赖热力图采集结果。四个板块模块的 `tradeDateBasis=CALENDAR`，`tradeDate` 仅为采集时参考交易日，不代表同花顺或东方财富的源数据日期；`FRESH` 也不表示源站发布了当日数据。大盘资金流使用东方财富源数据的 `日期`，`tradeDateBasis=SOURCE`。

## data 字段

| 模块 | 内容 |
| --- | --- |
| `industryHeatmap`、`conceptHeatmap` | 板块数组：`sectorCode`、`sectorName`、`sectorType`、`marketCap`、`changePercent`、`turnoverRate`、`riseCount`、`fallCount`、`leadingStockName`。 |
| `industryTop5`、`conceptTop5` | 对象含 `source="THS"`、`period="INTRADAY"`、`topRise`、`topFall`、`topInflow`、`topOutflow`。四个榜单各最多五项。 |
| `marketFundFlow` | `latest` 为最新可得交易日记录；`series` 为按日期升序排列的最多 20 条交易日记录。保留东方财富的主力、超大单、大单、中单、小单净流入及净占比，以及上证、深证收盘点位和涨跌幅。 |

Top5 四类榜单中的每项结构相同：

```json
{
  "sectorName": "半导体",
  "sectorType": "industry",
  "changePercent": 2.5,
  "netFlowAmount": 150000000
}
```

`sectorType` 只能是 `industry` 或 `concept`。同花顺的 `净额` 原单位为亿元，`netFlowAmount` 转换为元；它不是东方财富的主力净流入。涨幅榜按正涨跌幅降序，跌幅榜按负涨跌幅升序；流入榜按正净额降序，流出榜按负净额升序，即按流出绝对金额降序。同值时按 `sectorName` 字符串升序。空名、重复名和无效数值不进入榜单；若没有可用行，该模块本轮失败并按模块降级。V2 Top5 不含 `sectorCode`、`mainNetInflow`、`mainNetInflowRatio` 或 `unmatchedFundRows`。

金额及总市值单位为元；百分数字段的 `2.5` 表示 `2.5%`。无法确定的数值为 JSON `null`，不得用 `NaN`、`Infinity` 或伪造零值代替。大盘资金流仍遵循 AKShare 对东方财富数据的字段口径。

来源：[AKShare 股票数据文档](https://akshare.akfamily.xyz/data/stock/stock.html)。
