# be-data-analysis

AKShare 市场数据采集程序。每轮采集同花顺行业、概念与个股即时资金流，汇总后写入 Redis DB 2，供 `be-vita` 读取。前端不直接调用本程序。

## 环境

- Python 3.12
- Redis，需与 `be-vita` 连接同一个实例和 DB 2
- 本地开发与测试：`pip install -r requirements-dev.txt`；生产镜像只安装 `requirements.txt` 中的运行依赖
- 配置参考 `.env.example`。本地默认读取被 Git 忽略的 `.env.dev`；`APP_ENV=prod` 选择 `.env.prod`。运行时环境变量覆盖文件配置，文件中的密码不作变量展开

```bash
python -m app collect --force
python -m app serve
```

`serve` 启动健康接口与内置调度：北京时间交易日上午 09:30–11:30、下午 13:00–15:10 每两分钟尝试采集一次。两次市场启动至少120秒；快轮按定点节奏，耗时>=120秒的慢轮完成回收后若仍在交易窗口立即接续；不补历史时段、不重叠，非交易日由交易日历跳过。locked/throttled等快速退出等待下一定点，不忙循环。`collect --force` 可在采集窗口内手动触发，但仍受 Redis 锁、120 秒最小间隔和源冷却限制。

交易日历通过 AKShare 新浪接口独立刷新后缓存在 Redis DB 2；启动时补建，每月 1 日北京时间 00:10 常规刷新，失败自动重试每天最多一次。缓存未覆盖当天时不会凭工作日推断交易日，市场快照降级、个股跳过。`python -m app calendar-refresh` 可手动触发受限频保护的自动刷新。受保护的同步手动任务接口见 [交易日历与内部任务 V1](docs/trading-calendar-jobs-v1.md)。

本机 scheduler 使用 `/scheduler/api/local/market-data/v1` 下的八个固定 POST 业务入口，真实回环直连无需令牌；Java 调用 Python 内部接口仍需 `X-Internal-Token`。八项映射、同步结果与等待上限见上述文档。

第三方接口说明可通过 `python -m app sources` 查看，`--json` 输出完整静态元数据；命令不读取配置、不连接 Redis、不请求源站。13 个唯一函数、14 种参数组合及只读源验证/完整刷新区别见 [数据源与手动健康验证](docs/source-health-check.md)。

## 服务器容器部署

在服务器的 `be-data-analysis` 目录准备 `.env.prod`（参考 `.env.example`），其中 `REDIS_URL` 必须是容器可访问的地址；宿主机 Redis 使用 `REDIS_URL=host.docker.internal`，并通过 `REDIS_PORT`、`REDIS_DB` 指定端口和库。Compose 已配置 `host-gateway` 映射，并通过 `env_file` 自动将 `.env.prod` 配置注入运行容器；配置文件由 `.dockerignore` 排除，不进入镜像。然后执行：

```bash
docker compose up -d --build

curl -f http://127.0.0.1:18801/health
tail -f /data/logs/be-data-analysis/service.log
```

`compose.yaml` 将宿主机 `/data/logs/be-data-analysis/` 挂载到容器 `/app/logs/`。服务与定时采集子进程的标准输出、错误和 Uvicorn 日志都写入 `service.log`。容器以 UID/GID 10001 运行，因此宿主机日志目录须对该用户可写。应用端口映射为宿主机 18801 到容器 8000；`compose.yaml` 中的 `SERVICE_PORT` 固定容器监听端口为 8000。查看服务状态用 `docker compose ps`，重启用 `docker compose restart`。

生产镜像不安装 `pytest/httpx` 测试依赖，测试目录也不进入构建上下文。修改 `.env.prod` 后执行 `docker compose up -d` 重新创建容器以加载新配置；`restart` 不重新读取 env 文件。

日志轮转配置见 `deploy/logrotate.conf`，可复制到服务器 `/etc/logrotate.d/be-data-analysis`。配置使用 `copytruncate`，无需重启服务即可轮转正在写入的文件。

重叠运行由 Redis 锁阻止。市场普通网络、读取超时、解析或数据校验失败只暂停失败模块五分钟；明确403/429或有证据的风控拒绝才暂停共享源两小时；ETF 普通网络、超时、格式及标准化错误冷却五分钟。冷却跳过只记录 INFO 和剩余 TTL，不续期、不访问源站、不新增曲线点，已有有效数据保留为 STALE。

生产容器内存与 memory+swap 总额均为1GiB（不额外提供swap），cgroup总内存达到800MiB时停止准入并回收当前源进程；资源异常不触发源冷却。一个服务父进程启动两条固定通道：资金只取即时全市场资金榜；行情按股票报价→ETF→核心指数→行业→概念执行。同通道串行、启动至少相隔120秒，跨通道可并行；慢资金不阻塞行情，不补跑错过点。

行情入口为`stock:source-control:v1:entry:quotes`，资金入口为`stock:source-control:v1:entry:funds`。资料、字典、日历、资产配置使用行情入口；原完整market刷新原子获取两个入口，忙时立即locked/409，不遗留半把锁。所有源调用共用全局2路/同来源2路配额；租约30秒/每10秒续租，HTTP间隔、冷却、源超时不变，分页仍串行。资金通道只读有效日历缓存，UNKNOWN时降级，由行情/月度日历任务补建。

每模块/每股完成立即发布。市场快照采用WATCH/MULTI/EXEC，只合并本次变化模块；首次尝试后最多3次冲突重试，每次重新读最新快照/版本。资金点、股票版本与通知仍在原监控锁及同一事务中，失败变化通知不造点。控制Redis或业务发布失败停止后续处理。详见[执行控制与源验证](docs/source-health-check.md)。

代码按`app/api`、`runtime`、`providers`、`calendar`、`market`、`stock_monitor`、`etf_monitor`分包；`main/cli/__main__`与`core`保留。Python只输出结构化JSON，不生成HTML或全局改写字符串；展示文本的转义、合法HTTP/HTTPS外链限制由实际渲染端处理，不把转义内容写入业务存储。

请求连接上限五秒，读取上限由 `SOURCE_TIMEOUT_SECONDS` 控制（默认十五秒），保留源接口原有更短限制。同花顺分页至少间隔一秒，ETF 资料请求至少间隔两秒。日志包含上海时间、PID、任务、接口和模块耗时，已知 Redis 故障仅记录脱敏分类，分页进度条关闭。

同花顺个股资金的必需字段为涨跌幅、流入和流出；缺失或冲突时整批失败。有效批次净额始终以流入减流出计算，源净额可缺失，仅对具有有效源净额的同一股票子集作审计。标准化失败日志包含原因、字段、六位代码及异常行数，不包含原始字段值。

健康接口为 `GET /health`，仅对 Redis 执行 PING（连接和读取各一秒，无重试），不可用时返回 HTTP 503，不调用行情源。调度业务在服务父进程内执行；每个实际源调用独立spawn子进程。个股和 ETF 内部同步接口需 `X-Internal-Token` 鉴权。

个股监控 V1 使用独立的交易所股票字典和雪球资料/报价。`STOCK_MONITOR_XQ_ENABLED` 默认 `false`；关闭时自动调度和手动 `python -m app monitor-sample` 均不访问雪球，资料接口也拒绝调用。明确开启且配置 `XUEQIU_TOKEN` 后，服务在交易日盘中每两分钟对 Redis enabled 清单中的最多 10 只股票采样。配置、接口样例、Redis 键及频控见 [个股监控 V1](docs/stock-monitor-v1.md)。

市场快照另含新浪五只核心指数模块。ETF 监控独立使用新浪交易价格、同花顺基金基本资料，以及受总闸控制的雪球资产配置；盘中每两分钟对最多 10 只已启用 ETF 采样。同花顺资料独立同步，不受雪球总闸限制，采用 30 分钟资料限频与 180 秒批次预算。数据源、Redis 键、内部接口与不可用字段见 [核心指数与 ETF V1](docs/index-etf-sources-v1.md)。

同花顺三组即时接口没有可靠源交易日期或源时间；快照的 `tradeDate` 仅依据交易日历，`lastSuccessAt` 和市场曲线的 `collectedAt` 是采集时间。

## Redis 契约

快照键是 `stock:market:v1:snapshot`，值为普通 UTF-8 JSON 且不设 TTL；快照、同批已启用股票的资金点和更新通知在同一 Redis 事务中提交。市场与个股各有版本 ID，通知供读取方生成增量事件并在缺口时重同步。锁键是 `stock:market:v1:lock`。字段、单位和状态说明见 [V1 快照契约](docs/market-snapshot-v1.md)。

## 验证

```bash
python -m compileall app
python -m pytest
```

测试使用固定 DataFrame 和假 Redis，不依赖行情源或真实 Redis。AKShare 公开接口可能变化，部署前应运行一次受控联调，检查实际字段、最新交易日期及源站频率限制。
