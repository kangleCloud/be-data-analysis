# 个股监控 V1：Python 采集端

交易所代码名称清单独立取自上交所主板 A 股与科创板、深交所 A 股、北交所。雪球只用于已监控股票的基础资料和报价，通过固定版本 AKShare 的 `stock_individual_basic_info_xq` 与 `stock_individual_spot_xq` 读取。跨端字段与公开大屏形状以 `be-data-stock/doc/stock-monitor-v1.md` 为准。

## 配置与调用

`STOCK_MONITOR_XQ_ENABLED=false` 为默认值。关闭时不会启动每两分钟的雪球采样调度，`python -m app monitor-sample` 会直接跳过，内部 `profiles` 接口返回 503，均不会创建雪球客户端或访问雪球。授权确认并明确开启后，还需配置 `XUEQIU_TOKEN`。两个内部接口均要求 `STOCK_MONITOR_INTERNAL_TOKEN` 环境变量和同值的 `X-Internal-Token` 请求头；未配置令牌时接口不可用。令牌、Redis 密码不写日志。

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

交易所接口单次任一市场失败时整体报错，不返回不完整的全 A 股字典；资料接口单次任一股票失败时整体报错，避免 MySQL 写入半套资料。两个路径只供内网服务调用。交易所公开源实测完整清单获取需数十秒，调用方读取超时建议至少 60 秒。

## Redis DB 2

Python 只读 `stock:monitor:v1:enabled`（按排序的最多 10 只 `{symbol,code,name,market}`）；Java 在监控清单改变后写入。Python 仅在雪球开关开启且交易日盘中每两分钟、收盘后 15:02/04/06/08/10 运行 `monitor-sample` 子进程，并写：

- `stock:monitor:v1:quote:{symbol}`：`{schemaVersion:1,symbol,source:"XQ",sourceTime,collectedAt,tradeDate,price,changePercent,amount,low,high,open,limitUp,limitDown,averagePrice,volume,previousClose,status}`；新增数值字段均可为 `null`，价格单位元、成交量单位股；失败时保留有效历史报价为 `STALE`，没有历史报价为 `ERROR`。
- `stock:monitor:v1:series:{tradeDate}:{symbol}`：真实雪球源时间点 `[{time,price}]`；同一源时间去重，不补点，不制造午间点。
- `stock:monitor:v1:lastTradeDate`：最近实际采样交易日 `YYYY-MM-DD`，进入下一交易日后清理旧日期曲线。

同一实例或跨实例采样与资料请求由 `stock:monitor:v1:sample:lock` 排他；雪球 403、429 或令牌失效进入 `stock:monitor:v1:xq:cooldown` 两小时冷却。不同股票请求至少间隔一秒；同一股票两次报价请求由 Redis 原子 TTL 保证至少间隔 120 秒。收盘后仅源日期为当日且源时间严格晚于 15:00:00 才追加收盘点，该股确认后停止重试；未确认时保留有效历史并标 `STALE`，15:10 后停止请求。行情源时间取自雪球响应并按上海时区解析；金额和价格以元、涨跌幅以百分数传递，缺失数值用 `null`。

本仓库测试全部模拟交易所、雪球和 Redis，不向雪球发真实请求。可使用 `python -m pytest` 与 `python -m compileall app` 验证。
