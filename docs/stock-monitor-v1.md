# 个股监控 V1：Python 采集端

本说明纳入[v1.0.0工作树文档归档](../../docs/releases/v1.0.0.md)；公开V1及`schemaVersion=1`保持不变，归档不等同生产发布。

交易所代码名称清单独立取自上交所主板 A 股与科创板、深交所 A 股、北交所。雪球只用于已监控股票的基础资料和报价，通过固定版本 AKShare 的 `stock_individual_basic_info_xq` 与 `stock_individual_spot_xq` 读取。当前固定AKShare 1.19.1。跨端字段与公开大屏形状见[后端个股监控契约](../../be-data-stock/docs/development/stock-monitor-v1.md)。

## 配置与调用

`STOCK_MONITOR_XQ_ENABLED=false` 为默认值。服务仍建立四个固定调度任务，关闭时个股任务返回disabled且不访问雪球；`python -m app monitor-sample` 会直接跳过，内部 `profiles` 接口返回 503，均不会创建雪球客户端或访问雪球。授权确认并明确开启后，还需配置 `XUEQIU_TOKEN`。两个内部接口均要求 `STOCK_MONITOR_INTERNAL_TOKEN` 环境变量和同值的 `X-Internal-Token` 请求头；未配置令牌时接口不可用。令牌、Redis 密码不写日志。

Spring 每日整体刷新时调用交易所字典接口：

```http
POST /internal/stock-monitor/v1/exchange-dictionary
X-Internal-Token: <服务令牌>
Content-Type: application/json

{}
```

```json
{"schemaVersion":1,"stocks":[{"symbol":"SH600000","code":"600000","name":"浦发银行","market":"SH"}]}
```

仅在雪球生产开关开启时，Spring 可对最多 10 只不重复的 A 股代码调用资料接口：

```http
POST /internal/stock-monitor/v1/profiles
X-Internal-Token: <服务令牌>
Content-Type: application/json

{"symbols":["SH600000"]}
```

```json
{"schemaVersion":1,"profiles":[{"symbol":"SH600000","industry":"银行","listingDate":"1999-11-10","marketCap":123456789,"updatedAt":"2026-09-28T15:30:00+08:00"}]}
```

交易所接口单次任一市场失败时整体报错，不返回不完整的全 A 股字典；资料接口单次任一股票失败时整体报错，避免 MySQL 写入半套资料。两个路径只供内网服务调用。交易所字典串行四组合，每次源预算60秒（包含配额等待和回收）；调用方等待必须覆盖整个批次，不能将单次HTTP超时当作整批预算。当前源健康与生产耗时未实测。

## Redis DB 2

Python 只读 `stock:monitor:v1:enabled`（按排序的最多 10 只 `{symbol,code,name,market}`）；Java 在监控清单改变后写入。服务父进程在交易日盘中及收盘后15:02/04/06/08/10按固定120秒周期执行个股流程，受雪球开关和Token保护；每次实际源调用独立spawn子进程，最多三股在途，完成即标准化/发布，不通过CLI子进程嵌套调度。同任务不重叠、不积压、不补采。`python -m app monitor-sample`是复用同流程的单轮auto命令。报价与曲线键如下：

交易日判断读取与市场采集共用的 `stock:calendar:v1:trading-days`。若当天不在缓存覆盖范围，状态为 `UNKNOWN`，本轮跳过且不访问雪球；缓存命中非交易日也跳过。

- `stock:monitor:v1:quote:{symbol}`：`{schemaVersion:1,symbol,source:"XQ",sourceTime,collectedAt,tradeDate,price,changePercent,amount,low,high,open,limitUp,limitDown,averagePrice,volume,previousClose,marketCap,status}`；新增数值字段均可为 `null`，价格单位元、成交量单位股；失败时保留有效历史报价为 `STALE`，没有历史报价为 `ERROR`。
- `stock:monitor:v1:series:{tradeDate}:{symbol}`：真实雪球源时间点 `[{time,price}]`；同一源时间去重，不补点，不制造午间点。
- `stock:monitor:v1:lastTradeDate`：最近收到有效当日报价的交易日 `YYYY-MM-DD`；价格曲线仅保留最近两个有数据交易日，延迟或跨日报价不会清理曲线。
- `stock:monitor:v1:fund-series:{tradeDate}:{symbol}`：复用市场采集的同批同花顺个股资金 DataFrame，按采集时间写入 `[{collectedAt,inflow,outflow,netAmount}]`，金额单位元且净额为流入减流出；仅为同批出现的已启用股票写点，不增加逐股源请求。仅保留最近两个有数据交易日，缺样不补点。该曲线与雪球价格曲线独立。
- `stock:monitor:v1:state-id`：每次个股报价/价格曲线写入或资金点写入生成唯一状态 ID。业务键、状态 ID 与 `stock:monitor:v1:updates` 通知在同一 Redis 事务提交；通知为 `{baseStateId,stateId,changedSymbols}`。旧状态无 ID 时 `baseStateId=null`，读取方应重同步全量数据。

AUTO个股采样及资料共用`stock:source-control:v1:entry:monitor`入口和`stock:monitor:v1:sample:lock`业务锁；全局6、THS/XQ各3、其他来源各2个名额。明确401/403/429或认证/风控沿用`stock:source-control:v1:risk:xq`两小时保护，NX写入不续期；未知原因的旧`stock:monitor:v1:xq:cooldown`键等待自然到期。普通故障不扩大为共享认证冷却。首页与报价JSON每次实际HTTP均至少间隔一秒，这两次请求不是重试；同股首个HTTP用`stock:monitor:v1:sample:lastRequest:{symbol}`原子保证至少120秒，剩余<=5秒可在原预算内等待。父流程优先到期股，最多等本轮开始后60秒，计入300秒预算，不占源名额。延迟、跨日及重复的有效源报价只将最近有效报价标记 `STALE`，不追加采样点；字段无效仍记失败。收盘后仅源日期为当日且源时间严格晚于 15:00:00 才追加收盘点，该股确认后停止重试；未确认时保留有效历史并标 `STALE`，15:10 后停止请求。行情源时间取自雪球响应并按上海时区解析；金额和价格以元、涨跌幅以百分数传递，缺失数值用 `null`。

MANUAL按显式请求隔离执行器，跳过AUTO入口/业务锁、配额、普通冷却及代码间隔；共享HTTP限速、风险/未知旧保护、授权总闸和事务写入保护仍生效。历史保留、严格源时间>15收盘及最近两有数据交易日不变，详见[源说明](source-health-check.md)。

仓库已删除tests目录，不提供默认`python -m pytest`测试入口。临时离线验证位于`/tmp`；2026-10-14已交付155项通过记录，本轮文档归档未重跑。历史命令、覆盖及未实测边界见[归档验证状态](../../docs/releases/v1.0.0.md)。语法检查使用`python -m compileall app`，本轮仅核对文档与代码，不执行业务采样。
