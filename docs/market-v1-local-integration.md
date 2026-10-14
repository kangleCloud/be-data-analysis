# 市场看板 V1 本地联调

本说明纳入[v1.0.0工作树文档归档](../../docs/releases/v1.0.0.md)；公开V1及`schemaVersion=1`保持不变，归档不等同生产发布。

1. 在 `be-data-analysis` 根目录配置被 Git 忽略的 `.env.dev`，确认 `REDIS_URL`、`REDIS_PASSWORD`、`REDIS_PORT`、`REDIS_DB=2` 指向专用联调 Redis。
2. 在交易日北京时间 09:30–11:30 或 13:00–15:10，进入`.venv`后运行 `python -m app collect`，应用自动读取`.env.dev`。该CLI使用auto，仍受分布式锁、120秒间隔与普通冷却约束；本机scheduler手动刷新显式传manual，详细模式与风控约束见source-health-check.md。
3. 读取 `stock:market:v1:snapshot`，确认顶层 `schemaVersion=1`、`provider=akshare`、`generatedAt`；完整刷新四模块为`industrySectors`、`conceptSectors`、`marketFundFlow`、`coreIndices`，各模块 `tradeDateBasis=CALENDAR`。
4. 核对行业、概念 `data.items` 保留完整有效板块，金额单位元，`netFlowRate` 是净额占总流入流出的比例；市场 `latest` 是同花顺个股资金聚合，`series` 只包含当日成功采集点。`lastSuccessAt`、`collectedAt` 是采集时间，不得显示成源站时间。
5. 等待至少 120 秒再执行一次，确认成功时市场曲线增加真实采样点；任一源失败时该模块保留符合当前契约的旧数据并标 `STALE`，没有旧数据标 `ERROR/data=null`。失败轮次不得追加市场曲线点。

源接口通过 AKShare 调用同花顺，自动化测试使用 fake DataFrame 与 Redis，不依赖外网。当前固定AKShare1.19.1，日历、指数、行业、概念和资金保持各自校验；四个候选资金优先、按完成顺序发布。2026-10-14的155项离线验证是既有交付记录，本轮未执行上述可写采集命令；真实源健康/性能和生产部署仍未实测。不要在日志或测试数据中放入 Redis 凭据。
