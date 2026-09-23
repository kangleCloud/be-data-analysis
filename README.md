# be-data-analysis

AKShare 市场数据采集程序。每次运行采集行业、概念板块及资金流，写入 Redis DB 2，供 `be-vita` 读取。前端不直接调用本程序。旧股票和 ETF 日 K 查询接口已退出。

## 环境

- Python 3.12
- Redis，需与 `be-vita` 连接同一个实例和 DB 2
- `pip install -r requirements.txt`
- 配置参考 `.env.example`，生产连接地址和密码仅通过环境变量注入

```bash
python -m app collect
```

命令在交易日 09:30–11:30、13:00–16:00（北京时间）采集一次并退出。外部调度器在盘中每五分钟触发，收盘后至 16:00 再补采；午休、周末和非交易日自动跳过。手工补采可使用 `python -m app collect --force`，它允许在时段外采集，板块数据的交易日期取最近交易日历日期。

重叠运行由 Redis 锁阻止；锁占用时命令返回非零状态。任一模块失败时仍发布保留旧数据的快照，但命令返回非零状态，供调度器告警。运行日志不包含 Redis 凭据。

可选的健康接口使用 `python -m app serve` 启动，只提供 `GET /health`。它不执行采集。

交易时段可运行 `python -m app probe` 只读检查大盘资金流最新行的实际日期；命令不连接 Redis，也不写入快照。`sameCalendarDay=false` 时，后续页面须按 `latestTradeDate` 展示，不得称为当日实时资金流。

## Redis 契约

快照键是 `stock:market:v1:snapshot`，值为普通 UTF-8 JSON，单次 `SET` 原子发布，不设 TTL。锁键是 `stock:market:v1:lock`。字段、单位和状态说明见 [V1 快照契约](docs/market-snapshot-v1.md)。

## 验证

```bash
python -m compileall app
python -m pytest
```

测试使用固定 DataFrame 和假 Redis，不依赖行情源或真实 Redis。AKShare 公开接口可能变化，部署前应运行一次受控联调，检查实际字段、最新交易日期及源站频率限制。
