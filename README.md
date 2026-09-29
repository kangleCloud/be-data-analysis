# be-data-analysis

AKShare 市场数据采集程序。每轮采集同花顺行业、概念 Top5 和东方财富大盘资金流，写入 Redis DB 2，供 `be-vita` 读取。前端不直接调用本程序。

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

`serve` 启动健康接口与内置调度：北京时间工作日 09:40、10:10、10:40、11:10、13:10、13:40、14:10、14:40、15:30 分别通过独立子进程采集一次；已错过的时段不补跑，非交易日由交易日历跳过。`collect --force` 可在时段外手动采集，但仍受 Redis 时段占位、全局 20 分钟最小间隔及源冷却限制。

## 服务器容器部署

在服务器的 `be-data-analysis` 目录准备 `.env`（参考 `.env.example`），其中 `REDIS_URL` 必须是容器可访问的地址；宿主机 Redis 可使用 `redis://host.docker.internal`，并通过 `REDIS_PORT`、`REDIS_DB` 指定端口和库。然后执行：

```bash
sudo install -d -o 10001 -g 10001 -m 0755 /data/logs/be-data-analysis
docker compose up -d --build
curl -f http://127.0.0.1:8000/health
tail -f /data/logs/be-data-analysis/service.log
```

`compose.yaml` 将宿主机 `/data/logs/be-data-analysis/` 挂载到容器 `/app/logs/`。服务与定时采集子进程的标准输出、错误和 Uvicorn 日志都写入 `service.log`。容器以 UID/GID 10001 运行，因此宿主机日志目录须对该用户可写。应用端口默认映射到宿主机 8000，可在 `.env` 中设置 `SERVICE_PORT` 更改宿主机端口；容器内固定监听 8000。查看服务状态用 `docker compose ps`，重启用 `docker compose restart`。

日志轮转配置见 `deploy/logrotate.conf`，可复制到服务器 `/etc/logrotate.d/be-data-analysis`。配置使用 `copytruncate`，无需重启服务即可轮转正在写入的文件。

重叠运行由 Redis 锁阻止。源被限流、断连或超时后冷却两小时；同花顺分页请求至少间隔一秒。任一模块失败时仍发布保留旧成功数据的快照，但命令返回非零状态，需检查日志。日志记录接口、模块和总耗时以及异常类型，不包含 Redis 凭据。

健康接口为 `GET /health`；采集由服务内调度启动的一次性子进程执行。另有两个仅供 Spring 使用、需 `X-Internal-Token` 鉴权的个股监控内部接口。

个股监控 V1 使用独立的交易所股票字典和雪球资料/报价。`STOCK_MONITOR_XQ_ENABLED` 默认 `false`；关闭时自动调度和手动 `python -m app monitor-sample` 均不访问雪球，资料接口也拒绝调用。明确开启且配置 `XUEQIU_TOKEN` 后，服务在交易日盘中每两分钟对 Redis enabled 清单中的最多 10 只股票采样。配置、接口样例、Redis 键及频控见 [个股监控 V1](docs/stock-monitor-v1.md)。

交易时段可运行 `python -m app probe` 只读检查大盘资金流最新行的实际日期；命令不连接 Redis，也不写入快照。`sameCalendarDay=false` 时，后续页面须按 `latestTradeDate` 展示，不得称为当日实时资金流。

## Redis 契约

快照键是 `stock:market:v1:snapshot`，值为普通 UTF-8 JSON，单次 `SET` 原子发布，不设 TTL；写入成功后向 `stock:market:v1:updates` 发送更新通知。锁键是 `stock:market:v1:lock`。字段、单位和状态说明见 [V1 快照契约](docs/market-snapshot-v1.md)。

## 验证

```bash
python -m compileall app
python -m pytest
```

测试使用固定 DataFrame 和假 Redis，不依赖行情源或真实 Redis。AKShare 公开接口可能变化，部署前应运行一次受控联调，检查实际字段、最新交易日期及源站频率限制。
