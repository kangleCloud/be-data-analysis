# 夜间量化研究内部接口

内部路由使用 `X-Internal-Token`，其令牌复用已有个股监控内部令牌。路由只提供收盘后完整日线研究，不处理盘中、分钟线及自动交易。Java 服务负责保存请求、响应与证据版本；Python 不直接写入 MySQL。未执行真实上游数据验证。

## 当前证券池

`POST /internal/quant/v1/universe` 接收 `{"assetType":"STOCK"}` 或 `{"assetType":"ETF"}`。返回 `assetType`、`source`、`sourceFunction`、`requestedAt`、`rows`、`quality`；每行包括 `symbol`、`name`、`assetType`、`group`、`benchmark`。股票仅保留沪深主板代码，ETF 的分类及基准待核验，均为空。

列表为获取时的当前快照，质量为 `LIMITED`，不能作为历史 ST、停复牌、上市退市或 ETF 历史类别的证明。证券是否可以用于某次计算由上游提供可审计的历史资格证据。

## 历史日线

`POST /internal/quant/v1/history` 请求字段：`assetType` 为 `STOCK`、`ETF` 或 `INDEX`；股票/ETF 的 `symbol` 为六位代码，指数为小写 `sh`、`sz` 或 `csi` 加六位代码，如 `sh000300`；`frequency` 固定 `daily`；`adjust` 为 `raw`、`qfq` 或 `hfq`，指数只支持 `raw`；`start` 和 `end` 为 `YYYY-MM-DD`。日期不能位于未来，当天日线必须收盘后获取。

响应包含请求维度、`source`、`sourceFunction`、`requestedAt`、`rows` 和 `quality`，每行 `time`、`open`、`high`、`low`、`close`、`volume`、`amount`。价格数值不能用于直接推断历史当时的数据版本。`requestedAt` 是这次获取时间，不是历史行情当时可见时间；返回 `LIMITED`，需要另外核实历史池、完整性及复权/权益事件。空结果、字段缺失、价格或成交量异常不会伪造成成功数据。

## 因子与排名

`POST /internal/quant/v1/factors` 请求：

- `asOf` 为目标交易日期；`signalAt` 为该日 15:00 之后的上海时间戳，且不得晚于当前时刻，例如 `2024-06-03T18:00:00+08:00`。
- `dataVersion` 为保存输入数据的不可变版本标识。
- `tradeDates` 为覆盖所需历史窗口并以 `asOf` 结束的完整、递增交易日历；`calendarVerified` 必须为 `true`。标的或基准日线出现缺口时仅保留缺口之后的连续交易日尾段，不补造价格或成交额，窗口长度不足的因子不可用。
- `benchmarks` 是数组，每项 `symbol`、`qualityVerified`、`bars`；只有明确验证为 `true` 的基准可参与相对收益。
- `items` 为完整的可比证券池数组。每项 `assetType`、`symbol`、`group`、`benchmark`、`qualityVerified`、`universeVerified`、`securityEligible`、`bars`。标的或历史池未验证、或者不具备交易资格时不得将对应标志设为 `true`；三项均为 `true` 才计算因子和评分。股票和 ETF 不合并排名，ETF 按 `group` 划分。
- 每条 `bars` 包括 `date`（交易日期）、`close`、`amount`（证券因子需要）、`availableAt`（带时间的真实可见时间）；基准无需 `amount`。`date` 仅可为日期，`availableAt` 不可省略。每条数据只在不晚于 `signalAt` 时参与计算，不能以历史下载时间代替当时数据可见性。

响应包括 `asOf`、`signalAt`、`dataVersion`、`factorVersion`、`calendarHash`、`computedAt`、`factorDefinitions` 和完整 `items`。每只标的包含原始 `factors` 及三个周期的 `scores`；分数的 `components` 保存原值、百分位和权重。百分位以同类型同组、通过该策略硬条件且所有必需因子均可用的证券为比较集合，平分名次使用平均名次；组内仅一只时为 1。每个策略 `rank` 为该同类组内按综合分降序的名次，相同分数并列且下一名次跳位；未入选为 null。缺乏数据时不填零也不重分配权重，策略 `eligible=false`、`score=null`。整体质量始终为 `LIMITED`；`CALLER_ASSERTED` 仅表示调用者作出声明，不代表本服务独立验证。

其中波动率为日收益的样本标准差，回撤为当前收盘相对窗口最高收盘的跌幅；相对收益使用证券同一前后两个交易日期的基准收盘。调用方必须独立确认连续行情、股息权益、交易日和完整证券池；仅凭布尔声明不能证实历史真实性。因子和排名为研究参考，不保证收益。

## 分批计算与全池排名

`POST /internal/quant/v1/factors/raw` 接收前述因子请求及唯一 `batchId`，只返回该批原始因子，不做排名；每个条目的 `factors` 为因子名到数值或 null 的紧凑映射。

`POST /internal/quant/v1/factors/rank` 接收 `batches`（所有 raw 响应）、`expectedBatchIds`、`expectedUniverse`（每项 `assetType`、`symbol`、`group`），以及可选 `offset` 和 `limit`。批次、证券池、日期、信号时间、数据版本、因子版本或交易日历哈希不一致时拒绝。该清单由调用者提供，不代表服务器已核验证据。

排名在全池统一计算，结果分页仅用于传输；默认 `offset=0`、`limit=200`，最大 `limit=500`。响应包含 `total`、`offset`、`limit`、`hasMore` 和 `sourceBatchIds`；调用方必须取得并保存所有页面才能形成完整排名，不能以单页充当全部结果。每次分页均需提供相同的全量批次与清单。2000 个模拟标的的本机离线测试中，请求约 1.67 MB，默认第一页约 603 KB；不代表所有市场或标的数量一定满足传输上限。

## 回测

`POST /internal/quant/v1/backtest` 将 JSON 交由 `app.quant.backtest.run_backtest`，返回 `LIMITED` 或 `INVALID`。完整字段、数据证据和撮合限制见 [backtest.md](backtest.md)。
