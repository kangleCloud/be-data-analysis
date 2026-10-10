# Repository Guidelines

## 全局开发原则

项目处于早期阶段，暂无需要维持的稳定 API 或历史兼容约束。优先保持领域语义清晰、职责单一；结构不合理时直接重构，不要叠加 `legacy fallback`、兼容开关或重复实现。删除死代码和无效分支，只为数据假设、业务规则及异常边界添加必要中文注释。

## 项目结构与模块组织

项目使用 Python 3.12。根目录`app/main.py`、`cli.py`、`__main__.py`提供服务与命令入口；`core`保留配置/日志。`api`仅放内部路由；`runtime`放固定双通道工作流、调度、入口租约、源执行与资源保护；`providers`隔离第三方调用、HTTP控制、THS分页审计与纯静态源清单。`calendar`、`market`、`stock_monitor`、`etf_monitor`分别放领域采集、标准化、存储及相关时段规则，不添加legacy导入别名。测试放在`tests/`。

行情与资金为两条固定通道：同通道串行、跨通道并行，全局/同来源最多2个源进程。只有市场即时资金榜走资金通道，其他源走行情通道；完整market刷新原子取得两个入口。市场发布只合并本次模块，WATCH冲突最多重试3次（含首次共4次），保持V1业务键、版本链、事务通知及公共GET/SSE契约。资金通道仅只读日历，未知时由行情/月度任务补建。容器预算1GiB，cgroup保护阈值800MiB，资源异常不触发源冷却。

新增市场数据时先在 Provider 中隔离源接口，再在纯函数中完成标准化。不要让 AKShare 的 DataFrame、中文列名或异常进入 Redis 契约。

## 构建、测试与开发命令

- `python3 -m venv .venv && source .venv/bin/activate`：创建并激活虚拟环境。
- `pip install -r requirements-dev.txt`：安装本地运行与测试依赖；生产镜像只安装 `requirements.txt`。
- `set -a && source .env.example && set +a`：加载本地示例配置。
- `python -m app collect`：执行一轮采集并退出。
- `python -m app sources --json`：纯静态输出源依赖，不读取配置或访问Redis/源站。
- `python -m app serve`：启动可选健康接口。
- `python -m compileall app`：执行提交前语法检查。
- `python -m pytest`：运行全部自动化测试。

## 编码风格与命名规范

遵循 PEP 8，使用 4 个空格和 UTF-8。模块、函数和变量使用 `snake_case`，类使用 `PascalCase`，常量使用 `UPPER_SNAKE_CASE`。公共函数提供类型标注和简短 docstring。路由 `summary`、`description`、字段说明和调用方可见文案统一使用中文。文件 I/O、网络访问与数据转换必须分层，优先编写无副作用、可独立测试的函数。

## 测试规范

统一使用 `pytest`，文件命名为 `test_<module>.py`，测试函数命名为 `test_<behavior>`。新增功能需覆盖成功、空结果、源字段变化、Provider 故障、部分失败、锁竞争和边界日期。外部行情源必须使用 fake、fixture 或 mock 隔离；测试不得依赖网络。修复缺陷时必须增加回归测试。

## 安全与外部数据源

Redis 密码等秘密只能通过环境变量注入，不得提交真实值或写入日志和测试数据。公开网页接口可能变化；客户端必须设置超时、控制频率。采集失败不得以空结果覆盖上次成功快照。

## 提交与 Pull Request 规范

使用 Conventional Commits，例如 `feat(collector): add akshare snapshot`、`fix(snapshot): preserve stale data` 和 `docs: update usage`。每次提交只处理一个主题。Pull Request 应说明目的、Redis 契约或配置影响、数据源假设及已执行的测试；契约变化需附快照示例。不得提交真实业务数据、个人信息、token 或本地环境文件。
