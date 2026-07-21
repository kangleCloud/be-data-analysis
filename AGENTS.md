# Repository Guidelines

## 全局开发原则

项目处于早期阶段，暂无需要维持的稳定 API 或历史兼容约束。优先保持领域语义清晰、职责单一；结构不合理时直接重构，不要叠加 `legacy fallback`、兼容开关或重复实现。删除死代码和无效分支，只为数据假设、业务规则及异常边界添加必要中文注释。

## 项目结构与模块组织

项目使用 Python 3.12 和 FastAPI。`app/main.py` 负责应用装配；`app/api/` 仅处理 HTTP 参数和响应；`app/service/` 编排行情获取及标准化；`app/providers/baidu_finance/` 隔离百度请求和字段处理，`app/providers/eastmoney_symbol/` 只负责名称解析；`app/models/` 保存领域模型；配置、日志、异常和统一响应位于 `app/core/`。测试放在 `tests/`，Postman 样例放在 `postman/`。

新增真实数据源时实现 `MarketDataProvider`，并在 Provider 工厂注册。不要让第三方字段、Cookie 或异常直接进入 API 层。

## 构建、测试与开发命令

- `python3 -m venv .venv && source .venv/bin/activate`：创建并激活虚拟环境。
- `pip install -r requirements.txt`：安装运行与测试依赖。
- `set -a && source .env.example && set +a`：加载本地示例配置。
- `python -m app`：在默认 `8000` 端口启动服务。
- `python -m compileall app`：执行提交前语法检查。
- `python -m pytest`：运行全部自动化测试。

## 编码风格与命名规范

遵循 PEP 8，使用 4 个空格和 UTF-8。模块、函数和变量使用 `snake_case`，类使用 `PascalCase`，常量使用 `UPPER_SNAKE_CASE`。公共函数提供类型标注和简短 docstring。路由 `summary`、`description`、字段说明和调用方可见文案统一使用中文。文件 I/O、网络访问与数据转换必须分层，优先编写无副作用、可独立测试的函数。

## 测试规范

统一使用 `pytest`，文件命名为 `test_<module>.py`，测试函数命名为 `test_<behavior>`。新增功能需覆盖成功、空结果、参数非法、Provider 故障和边界日期。外部行情源必须使用 fake、fixture 或 mock 隔离；测试不得依赖网络。修复缺陷时必须增加回归测试。

## 安全与外部数据源

`BAIDU_AB_SR` 等秘密只能通过环境变量注入，不得提交真实值或写入日志、Postman 和测试数据。公开网页接口可能变化；客户端必须设置超时、有限重试并将故障统一映射为 502。名称解析与行情请求使用独立会话，禁止跨数据源转发 Cookie。

## 提交与 Pull Request 规范

使用 Conventional Commits，例如 `feat(provider): add akshare client`、`fix(api): reject invalid date range`、`docs: update usage`。每次提交只处理一个主题。Pull Request 应说明目的、接口或配置影响、数据源假设及已执行的测试；响应结构变化需附请求与响应示例。不得提交真实业务数据、个人信息、token 或本地环境文件。
