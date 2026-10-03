# be-data-analysis

AKShare 市场数据采集程序。每轮采集同花顺行业、概念与个股即时资金流，汇总后写入 Redis DB 2，供 `be-vita` 读取。前端不直接调用本程序。

## 环境

- Python 3.12
- Redis，需与 `be-vita` 连接同一个实例和 DB 2
- `pip install -r requirements.txt`
- 配置参考 `.env.example`。本地可在被 Git 忽略的 `.env` 中分别设置 `REDIS_URL`、`REDIS_PASSWORD`、`REDIS_PORT` 和 `REDIS_DB`；生产连接参数通过运行环境注入

```bash
set -a && source .env.dev && set +a
python -m app collect --force
python -m app serve
```

`serve` 启动健康接口与内置调度：北京时间交易日上午 09:30–11:30、下午 13:00–15:10 每两分钟尝试采集一次。上一轮未完成时不排队补跑；非交易日由交易日历跳过。`collect --force` 可在采集窗口内手动触发，但仍受 Redis 锁、120 秒最小间隔和源冷却限制。

交易日历通过 AKShare 新浪接口独立刷新后缓存在 Redis DB 2；启动时补建，每月 1 日北京时间 00:10 常规刷新，失败自动重试每天最多一次。缓存未覆盖当天时不会凭工作日推断交易日，市场快照降级、个股跳过。`python -m app calendar-refresh` 可手动触发受限频保护的自动刷新。受保护的同步手动任务接口见 [交易日历与内部任务 V1](docs/trading-calendar-jobs-v1.md)。

## 服务器容器部署

在服务器的 `be-data-analysis` 目录准备 `.env`（参考 `.env.example`），其中 `REDIS_URL` 必须是容器可访问的地址；宿主机 Redis 可使用 `redis://host.docker.internal`，并通过 `REDIS_PORT`、`REDIS_DB` 指定端口和库。然后执行：

```bash
sudo install -d -o 10001 -g 10001 -m 0755 /data/logs/be-data-analysis
docker compose up -d --build
curl -f http://127.0.0.1:18000/health
tail -f /data/logs/be-data-analysis/service.log
```

`compose.yaml` 将宿主机 `/data/logs/be-data-analysis/` 挂载到容器 `/app/logs/`。服务与定时采集子进程的标准输出、错误和 Uvicorn 日志都写入 `service.log`。容器以 UID/GID 10001 运行，因此宿主机日志目录须对该用户可写。应用端口映射为宿主机 18000 到容器 8000；`compose.yaml` 中的 `SERVICE_PORT` 固定容器监听端口为 8000。查看服务状态用 `docker compose ps`，重启用 `docker compose restart`。

日志轮转配置见 `deploy/logrotate.conf`，可复制到服务器 `/etc/logrotate.d/be-data-analysis`。配置使用 `copytruncate`，无需重启服务即可轮转正在写入的文件。

重叠运行由 Redis 锁阻止。源被限流、断连或超时后冷却两小时；同花顺分页请求至少间隔一秒。任一模块失败时仍发布保留旧成功数据的快照，但命令返回非零状态，需检查日志。日志记录接口、模块和总耗时以及异常类型，不包含 Redis 凭据。

健康接口为 `GET /health`；采集由服务内调度启动的一次性子进程执行。个股和 ETF 内部同步接口需 `X-Internal-Token` 鉴权。

个股监控 V1 使用独立的交易所股票字典和雪球资料/报价。`STOCK_MONITOR_XQ_ENABLED` 默认 `false`；关闭时自动调度和手动 `python -m app monitor-sample` 均不访问雪球，资料接口也拒绝调用。明确开启且配置 `XUEQIU_TOKEN` 后，服务在交易日盘中每两分钟对 Redis enabled 清单中的最多 10 只股票采样。配置、接口样例、Redis 键及频控见 [个股监控 V1](docs/stock-monitor-v1.md)。

市场快照另含新浪五只核心指数模块。ETF 监控独立使用新浪交易价格、交易所基金资料，以及受总闸控制的雪球资产配置；盘中每两分钟对最多 10 只已启用 ETF 采样。数据源、Redis 键、内部接口与不可用字段见 [核心指数与 ETF V1](docs/index-etf-sources-v1.md)。

同花顺三组即时接口没有可靠源交易日期或源时间；快照的 `tradeDate` 仅依据交易日历，`lastSuccessAt` 和市场曲线的 `collectedAt` 是采集时间。

## Redis 契约

快照键是 `stock:market:v1:snapshot`，值为普通 UTF-8 JSON 且不设 TTL；快照、同批已启用股票的资金点和更新通知在同一 Redis 事务中提交。市场与个股各有版本 ID，通知供读取方生成增量事件并在缺口时重同步。锁键是 `stock:market:v1:lock`。字段、单位和状态说明见 [V1 快照契约](docs/market-snapshot-v1.md)。

## 验证

```bash
python -m compileall app
python -m pytest
```

测试使用固定 DataFrame 和假 Redis，不依赖行情源或真实 Redis。AKShare 公开接口可能变化，部署前应运行一次受控联调，检查实际字段、最新交易日期及源站频率限制。
