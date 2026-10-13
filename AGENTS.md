# Repository Guidelines

## 全局开发原则

项目处于早期阶段，暂无需要维持的稳定 API 或历史兼容约束。优先保持领域语义清晰、职责单一；结构不合理时直接重构，不要叠加 `legacy fallback`、兼容开关或重复实现。删除死代码和无效分支，只为数据假设、业务规则及异常边界添加必要中文注释。

## 项目结构与模块组织

项目使用 Python 3.12。根目录`app/main.py`、`cli.py`、`__main__.py`提供服务与命令入口；`core`保留配置/日志。`api`仅放内部路由；`runtime`放四个固定采集任务工作流、调度、入口租约、源执行与资源保护；`providers`隔离第三方调用、HTTP控制、THS分页审计与纯静态源清单。`calendar`、`market`、`stock_monitor`、`etf_monitor`分别放领域采集、标准化、存储及相关时段规则，不添加legacy导入别名。离线验证按会话要求放在临时目录，不恢复已删除的测试目录。

个股、ETF、市场行情、全市场资金为四个独立120秒采集周期，各任务入口独立、同任务不重叠、不积压补跑。AUTO全局最多6个源名额，THS/XQ各3个、SINA/交易所各2个；个股最多三只在途，市场指数/行业/概念最多三项在途。各在途调用独立执行器及取消状态，通过copy_context传递入口租约，父流程独占标准化/事务发布。ETF一次新浪全表同时更新行情和字典缓存；固定即时资金相邻页最多两路，其他资料/字典批次串行。完整market刷新原子取得市场行情/资金两个入口。日历有独立入口；资金只读日历缓存，未知不能推断交易日。个股优先已到期项，父流程最多等待60秒（计入300秒，不占源名额），60秒内到期项可在并发位空闲后提交，较晚到期跳过。120秒门禁在实际首个HTTP启动时设置，剩余<=5秒在原预算内等待，较长跳过；同一长批次后续页不重置首请求门禁；额外资金页占一个全局/THS名额，拿不到则串行，按页交付原AKShare解析，父进程回收后按所有权清理额外租约；同批后续物理HTTP继续遵守共享源速率。MANUAL保留隔离及共享风控契约。市场WATCH冲突首次加3次重试，保持V1业务键/版本链/同事务通知及GET/SSE；不补造采样点。源异常退出不触发源冷却，保留退出码/信号及TERM/KILL回收，不引入内存监控。

新增市场数据时先在 Provider 中隔离源接口，再在纯函数中完成标准化。不要让 AKShare 的 DataFrame、中文列名或异常进入 Redis 契约。

## 构建、测试与开发命令

- `python3 -m venv .venv && source .venv/bin/activate`：创建并激活虚拟环境。
- `pip install -r requirements-dev.txt`：安装本地运行与测试依赖；生产镜像与原生部署只安装 `requirements.txt`。
- 参考`.env.example`配置私有`.env.dev`，应用自动加载；生产使用`APP_ENV=prod`选择私有`.env.prod`。
- `python -m app collect`：执行一轮采集并退出。
- `python -m app sources --json`：纯静态输出源依赖，不读取配置或访问Redis/源站。
- `python -m app serve`：启动可选健康接口。
- `python -m compileall app`：执行提交前语法检查。
- 离线模拟验证放在`/tmp`，外部源及Redis连接必须替换为fake/mock，不调用真实源或读取私有配置。

## 编码风格与命名规范

遵循 PEP 8，使用 4 个空格和 UTF-8。模块、函数和变量使用 `snake_case`，类使用 `PascalCase`，常量使用 `UPPER_SNAKE_CASE`。公共函数提供类型标注和简短 docstring。路由 `summary`、`description`、字段说明和调用方可见文案统一使用中文。文件 I/O、网络访问与数据转换必须分层，优先编写无副作用、可独立测试的函数。

## 测试规范

统一使用 `pytest`，文件命名为 `test_<module>.py`，测试函数命名为 `test_<behavior>`。新增功能需覆盖成功、空结果、源字段变化、Provider 故障、部分失败、锁竞争和边界日期。外部行情源必须使用 fake、fixture 或 mock 隔离；测试不得依赖网络。修复缺陷时必须增加回归测试。

## 安全与外部数据源

Redis 密码等秘密只能通过环境变量注入，不得提交真实值或写入日志和测试数据。公开网页接口可能变化；客户端必须设置超时、控制频率。采集失败不得以空结果覆盖上次成功快照。

## 提交与 Pull Request 规范

使用 Conventional Commits，例如 `feat(collector): add akshare snapshot`、`fix(snapshot): preserve stale data` 和 `docs: update usage`。每次提交只处理一个主题。Pull Request 应说明目的、Redis 契约或配置影响、数据源假设及已执行的测试；契约变化需附快照示例。不得提交真实业务数据、个人信息、token 或本地环境文件。

## 原生部署

Linux systemd部署文件位于`deploy/systemd/`，说明为`docs/native-systemd-deployment.md`。默认`/data/content/be-data-analysis`，默认root运行，其他账号直接修改unit与日志轮转账号，使用目标已有Python3.12虚拟环境，仅安装生产依赖；systemd仅保留APP_ENV=prod和日志无缓冲变量，业务配置统一沿用私有.env.prod，虚拟IP由部署填写，原生端口18801。stdout/stderr追加现有service.log，不新增Python文件handler。Docker入口保留，先停旧实例再启新实例；不在开发会话执行服务器命令。
