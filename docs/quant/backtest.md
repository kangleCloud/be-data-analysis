# 日频研究回测输入与限制

`app.quant.backtest.run_backtest` 接收 JSON 对象并返回 JSON 对象。只支持 `frequency=daily`、`signalTime=close`；第一个交易日的收盘信号最早于下一交易日开盘尝试买入，买入后最早下一交易日卖出。只支持一个资产类型和一个可比组，不同类型或组分别运行。

## 请求

- `strategy`：`short`、`medium`、`long`；`assetType`：`STOCK` 或 `ETF`；`group`：非空可比组标识；`dataVersion`、`factorVersion`：非空版本标识。
- `coverage`：`calendar`、`universe`、`statuses`、`limits`、`openLiquidity`、`corporateActions`、`benchmark`、`factors` 全为布尔 `true`，同时提供非空 `evidenceVersion`。这是调用方对数据覆盖的声明，并不替代外部证据核验。
- `costs`：`buyCommission`、`sellCommission`、`sellTax`、`slippage` 均为小于 1 的非负费率，`minCommission` 为单笔最低佣金（元）。不得省略或预置成本。
- `days`：交易日按日期递增，调用方负责提供完整交易日历；每一天提供 `date`、`benchmarkClose`、带时区的 `benchmarkAvailableAt` 和 `items`。
- `items`：每个交易日均包含整个回测区间证券集合的状态，上市前及退市后也需提供 `listed=false` 记录。每条包括 `symbol`、`assetType`、`group`、`listed`、`st`、`suspended`、`statusAvailableAt`、`corporateActions`。状态必须在当天 09:30 前可见，权益事件必须显式传入空数组；非空则拒绝。
- 在市证券还需提供不复权 `close`、`closeAvailableAt` 以及 `signal`：`eligible`、0 到 1 的 `score`（入选时必需）、`trendBelow`、`availableAt`；信号和收盘价只能当日 15:00 后可见。停牌证券的 `close` 由有证据的最近收盘价表示，不可用时禁止计算。
- 在市且不停牌证券还需提供不复权 `open`、`high`、`low`、`volume`（股）、`upperLimit`、`lowerLimit`；另外必须有已核验当日 09:30 时可执行的 `openBuyVolume`、`openSellVolume`（股）及 `openAvailableAt`。全天最高、最低、成交量只用于数据一致性检查，不用于判断开盘成交。日K 本身不能证明开盘可执行数量，缺少证据时应拒绝。
- 时间格式示例：`2024-01-02T09:30:00+08:00`。信号、行情、状态和基准的历史可见性均必须有独立依据；上游不得用最新修订数据冒充历史可见数据。

## 输出与撮合

- 缺少上述任意必需字段、历史数据不完整、有权益事件、输入非日频等情况返回 `{"status":"INVALID","reasons":["..."]}`。
- 满足输入约束后输出 `status=LIMITED`、`reasons`、`assumptions`、`orders`、`trades`、`equity`、`positions`、`metrics`；即使盘口数量有证据，开盘价格与滑点仍是近似撮合，无法保证排队成交。
- 每日按次日开盘依次尝试已有卖单和前一交易日信号买单，涨停买入与跌停卖出保守记作未成交，卖单继续等待；买单有效期为下一交易日。持仓上限两只，每只购买预算为上日净值的 45%，买入数量为 100 股整数倍。
- 从买入后第一个交易日起计持有天数；持有到策略上限时下一次可成交开盘退出，跌破均线在最短持有期后于收盘触发次日卖出。短/中/长收盘止损触发为 5%/8%/12%，组合净值较高点回撤 10% 暂停买入且不自动恢复。
- 不支持分红、拆分、配股等权益事件处理，存在事件的区间必须拒绝，不能当作零事件。持仓退市后无处置资料亦拒绝。跨时间股票池和指数成分、证据真实性、数据复权和因子重算由上游保证。
- `metrics` 年化按 252 个交易日、夏普无风险利率为零，波动或样本不足时值为 `null`；换手率为买卖成交额合计除以平均净值；`recoveryDays=null` 可能表示尚未恢复或没有回撤。
- 盈亏比是已平仓盈利交易平均收益率除以亏损交易平均收益率的绝对值，单笔收益率以含买入佣金的投入成本为分母，收益扣除买卖费用和卖出税费；无盈利或无亏损交易时 `profitLossRatio` 为 `null`，`profitLossRatioReason` 说明原因。
