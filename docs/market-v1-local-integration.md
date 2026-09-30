# 市场看板 V1 本地联调

1. 在 `be-data-analysis` 根目录配置被 Git 忽略的 `.env.dev`，确认 `REDIS_URL`、`REDIS_PASSWORD`、`REDIS_PORT`、`REDIS_DB=2` 指向专用联调 Redis。
2. 在交易日北京时间 09:30–11:30 或 13:00–15:10，执行 `set -a && source .env.dev && set +a`，随后运行 `python -m app collect --force`。手动触发仍受分布式锁、120 秒最小间隔和源冷却约束。
3. 读取 `stock:market:v1:snapshot`，确认顶层 `schemaVersion=1`、`provider=akshare`、`generatedAt`；模块恰为 `industrySectors`、`conceptSectors`、`marketFundFlow`，三者 `tradeDateBasis=CALENDAR`。
4. 核对行业、概念 `data.items` 保留完整有效板块，金额单位元，`netFlowRate` 是净额占总流入流出的比例；市场 `latest` 是同花顺个股资金聚合，`series` 只包含当日成功采集点。`lastSuccessAt`、`collectedAt` 是采集时间，不得显示成源站时间。
5. 等待至少 120 秒再执行一次，确认成功时市场曲线增加真实采样点；任一源失败时该模块保留符合当前契约的旧数据并标 `STALE`，没有旧数据标 `ERROR/data=null`。失败轮次不得追加市场曲线点。

源接口通过 AKShare 调用同花顺，自动化测试使用 fake DataFrame 与 Redis，不依赖外网。真实源字段及分页结果需在部署前做受控联调；不要在日志或测试数据中放入 Redis 凭据。
