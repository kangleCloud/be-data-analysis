# be-data-analysis

基于 FastAPI 的中国 A 股与场内 ETF 数据获取和标准化服务。默认通过百度财经公开接口获取历史日 K，并使用独立名称解析器将股票全名转换为六位代码。所有接口统一返回 `code/msg/data`。

## 项目结构

```text
app/
├── api/                         # HTTP 路由及 Pydantic 模型
├── core/                        # 配置、日志、异常和统一响应
├── models/                      # 数据源无关的领域模型
├── providers/
│   ├── baidu_finance/           # 百度客户端、Provider 和 processing
│   └── eastmoney_symbol/        # A 股名称精确解析
├── service/                     # 查询编排、过滤、排序和异常映射
└── main.py                      # FastAPI 应用工厂
postman/                         # 可导入的 Postman Collection
tests/                           # 无网络依赖的 pytest 测试
```

## 本地运行

项目使用 Python 3.12：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
set -a
source .env.example
set +a
python -m app
```

服务默认监听 `8000` 端口，中文 OpenAPI 文档位于 `http://127.0.0.1:8000/docs`。

## API 示例

按名称查询四方科技（`603339`）历史日 K：

```bash
curl -X POST 'http://127.0.0.1:8000/api/v1/stocks/history' \
  -H 'Content-Type: application/json' \
  -d '{"name":"四方科技","start_date":"2026-07-01","end_date":"2026-07-20"}'
```

查询最近交易日日线：

```bash
curl -X POST 'http://127.0.0.1:8000/api/v1/stocks/latest' \
  -H 'Content-Type: application/json' \
  -d '{"name":"四方科技"}'
```

现有按代码接口继续可用：

```bash
curl 'http://127.0.0.1:8000/api/v1/stocks/603339/history?start_date=2026-07-01&end_date=2026-07-20'
curl 'http://127.0.0.1:8000/api/v1/stocks/603339/latest'
curl 'http://127.0.0.1:8000/api/v1/funds/510300/history?start_date=2026-07-01&end_date=2026-07-20'
```

Postman 可直接导入 `postman/Baidu_Finance_API.postman_collection.json`。请求体中的 `name` 和 `symbol` 必须且只能提供一个；名称使用 A 股全名精确匹配。历史区间无数据时成功返回空列表，最新日线无数据时返回 404。

## 配置与安全

默认 `DATA_PROVIDER=baidu_finance`、`SYMBOL_RESOLVER=eastmoney`。如需完全离线联调，可同时设置为 `mock`。超时、重试和缓存配置见 `.env.example`。

`BAIDU_AB_SR` 是可选秘密配置。公开日 K 不依赖它；设置后只会写入发往 `finance.pae.baidu.com` 的 Cookie Jar。不要把真实值写入 `.env.example`、日志、Postman 或版本库。服务不生成 `ab_sr`、`acs-token`，也不提供受保护的盘中实时行情。

上述接口是网页使用的公开但非正式 API，可能变更。部署时应控制请求频率，并遵守数据提供方的使用条款和隐私政策。

## 开发检查

```bash
python -m compileall app
python -m pytest
```

外部数据源测试必须使用 fake 或 mock，不得访问网络。新增 Provider 应实现 `MarketDataProvider`，并将第三方字段转换限制在 Provider 的处理包内。
