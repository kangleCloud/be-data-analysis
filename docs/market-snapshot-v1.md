# 市场快照 V1 契约

本说明纳入[v1.0.0工作树文档归档](../../docs/releases/v1.0.0.md)；公开V1及`schemaVersion=1`保持不变，归档不等同生产发布。

`be-data-analysis` 向 Redis DB 2 的 `stock:market:v1:snapshot` 写入普通 UTF-8 JSON 字符串，不设 TTL。每次发布分配唯一 `snapshotId`。同一 Redis 事务写入快照并向 `stock:market:v1:updates` 发布 `{schemaVersion:1,snapshotId,previousSnapshotId,changedModules}`；有已启用个股资金点时，它们也在该事务中写入。`changedModules` 只列出相对上次快照内容变化的模块，含状态变化。旧缓存没有 `snapshotId` 时 `previousSnapshotId=null`，从新快照开始建立版本链。读取方可按版本获取快照并生成增量事件，遇缺口重新读取全量快照。

```json
{
  "schemaVersion": 1,
  "snapshotId": "示例唯一标识",
  "provider": "akshare",
  "generatedAt": "2026-09-30T10:00:00+08:00",
  "modules": {
    "industrySectors": {},
    "conceptSectors": {},
    "marketFundFlow": {},
    "coreIndices": {}
  }
}
```

各模块均包含 `status`、`tradeDate`、`tradeDateBasis`、`lastSuccessAt`、`lastAttemptAt`、`message`、`data`。本轮成功为 `FRESH`；失败但有符合当前契约的旧数据为 `STALE`；无旧数据为 `ERROR/data=null`。四个模块独立降级。三组同花顺源及新浪指数均没有可靠的逐条源日期和源时间，因此 `tradeDateBasis=CALENDAR`；`tradeDate` 仅是交易日历参考日期。`lastSuccessAt`、市场曲线的 `collectedAt` 与 `generatedAt` 是本服务采集时间，不代表源站时间。

## 行业与概念

`industrySectors`、`conceptSectors` 分别来自 AKShare `stock_fund_flow_industry(symbol="即时")` 和 `stock_fund_flow_concept(symbol="即时")`：

```json
{
  "source": "THS",
  "period": "INTRADAY",
  "items": [{
    "code": null,
    "name": "半导体",
    "type": "industry",
    "indexValue": 1234.5,
    "changePct": 2.31,
    "inflow": 200000000,
    "outflow": 50000000,
    "netAmount": 150000000,
    "netFlowRate": 60,
    "companyCount": 55,
    "leader": "示例股票",
    "leaderChangePct": 9.9,
    "leaderPrice": 23.5
  }]
}
```

`items` 保存完整的有效板块行，读取方计算排行：正涨幅降序前 10、负涨幅升序前 10、正净额降序前 10、负净额升序前 10。Python不裁剪为Top10。缺失字段返回 `null`，不伪造代码或零值。所有金额单位为元；百分数 `2.31` 表示 `2.31%`。`netFlowRate=netAmount/(inflow+outflow)*100`，分母缺失或非正数时为 `null`；它不是主力净占比。

## 五只核心指数

`coreIndices.data`为`{source:"SINA_INDEX",items:[...]}`，来自`stock_zh_index_spot_sina()`，固定顺序为上证指数`sh000001`、深证成指`sz399001`、沪深300`sh000300`、创业板指`sz399006`、科创50`sh000688`。缺任意一只或点位无效时整个模块降级。

每项包含`code/name/price/change/changePercent/previousClose/open/high/low/volume/amount/sourceTime/collectedAt/series`。`price`为指数点位，涨跌幅为百分数，`sourceTime=null`；曲线点为`{collectedAt,price}`，不将采集时间解释为源交易时间或收盘确认。窗口外无可靠源日期的手动刷新不生成日内曲线。完整字段与边界见[核心指数与ETF](index-etf-sources-v1.md)。

## 大盘资金

`marketFundFlow` 来自 AKShare `stock_fund_flow_individual(symbol="即时")` 的有效、去重股票行汇总：

```json
{
  "source": "THS_INDIVIDUAL_AGGREGATE",
  "reconciledFromLegacy": false,
  "latest": {
    "collectedAt": "2026-09-30T10:00:00+08:00",
    "inflow": 130000000,
    "outflow": 90000000,
    "netAmount": 40000000,
    "riseCount": 1,
    "fallCount": 1,
    "flatCount": 0,
    "stockCount": 2
  },
  "series": [{
    "collectedAt": "2026-09-30T10:00:00+08:00",
    "inflow": 130000000,
    "outflow": 90000000,
    "netAmount": 40000000
  }]
}
```

`netAmount` 始终由同批有效去重股票的 `inflow-outflow` 计算，`latest` 和 `series` 使用同一结果；源列“净额”仅用于记录差异，不直接作为产品净额。新采集 `reconciledFromLegacy=false`。个股资金表头为元：纯数值按元解析，带“万/亿”的字符串按显式单位换算；板块表头为亿，纯数值按亿元解析。关键金额缺失或同一股票出现冲突行时拒绝本轮市场汇总，保留旧成功数据。涨、跌、平数量之和必须等于有效去重股票数。曲线仅追加当日实际成功采样点，不补午间或失败点；新交易日重新开始。

同一轮全市场个股 DataFrame 也为已启用的最多 10 只股票生成 `stock:monitor:v1:fund-series:{tradeDate}:{symbol}` 资金点，格式为 `[{"collectedAt":"2026-09-30T10:00:00+08:00","inflow":100000000,"outflow":40000000,"netAmount":60000000}]`，单位元。横轴是采集时间，源未提供可靠的逐股时间；缺样留空，不补点。快照、资金点及通知在同一 Redis 事务中提交；新增资金点同时更新 `stock:monitor:v1:state-id` 并发布个股通知。资金曲线只保留最近两个有数据交易日。

## 采集与风控

`python -m app serve` 启动个股、ETF、市场行情、全市场资金四个独立120秒周期任务。市场行情包含指数/行业/概念三项，资金只调用即时个股榜；完整市场刷新原子获取quotes/funds两个入口，优先提交资金及三项行情共四个候选，按完成顺序由父流程标准化、WATCH合并及事务发布。每次源调用独立spawn子进程。同任务不重叠、不积压或补采。AUTO全局6、THS/XQ各3、SINA及各交易所各2个名额。

AKShare当前固定1.19.1；即时资金使用两页滑动窗口及两条线程独占的持久Session，额外名额拿不到即串行，完整审计后一次发布同批汇总与个股点。CLI `python -m app collect` 使用auto，不能绕过交易窗口、日历、锁、120秒间隔和冷却。同花顺每次实际HTTP启动至少间隔一秒。auto普通网络/超时/解析/数值校验仅当前模块冷却300秒，键为适用原模块键加`:ordinary`；401/403/429及明确风控两模式共享`stock:source-control:v1:risk:{group}`的7200秒保护，未知原因旧冷却等待自然到期。采集锁在长分页期间续租，发布前核验归属。窗口和取消规则见[采样调度](sampling-scheduling.md)。

交易日历由 `stock:calendar:v1:trading-days` 缓存提供。当天超出缓存日期范围时状态为 `UNKNOWN`，本轮不请求同花顺，旧成功模块降级为 `STALE`；没有旧数据则为 `ERROR`。上述时段、日历与锁约束适用于auto。内部POST显式`X-Collection-Mode: manual`复用同一采集逻辑，跳过普通冷却/间隔/采集锁/源配额，可窗口外刷新；无可靠源日期时tradeDate=null，只更新快照与实际采集时间、不新增日内点；HTTP共享限速、认证/风控、资源与事务保护保留。详情见[交易日历与内部任务 V1](trading-calendar-jobs-v1.md)。

来源：[AKShare 股票数据文档](https://akshare.akfamily.xyz/data/stock/stock.html)。
