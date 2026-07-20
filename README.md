# be-data-analysis

基于 FastAPI 的股票基金数据获取与处理服务。首版提供中国 A 股和场内 ETF 历史日线查询，使用可插拔 Provider 隔离具体数据源。

当前仅内置确定性的 `mock` Provider，不会访问真实行情服务。所有查询响应均包含 `provider: "mock"` 和 `mock_data: true`，仅用于本地开发与接口联调。

## 项目结构

```text
app/
├── api/          # 路由及 Pydantic API 模型
├── core/         # 配置、日志、异常和统一响应
├── models/       # 与数据源无关的领域模型
├── providers/    # Provider 协议、工厂及具体实现
├── service/      # 行情获取与标准化业务逻辑
├── main.py       # FastAPI 应用工厂
└── cli.py        # Uvicorn 启动入口
tests/            # pytest 自动化测试
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

启动后访问 `http://127.0.0.1:8000/docs` 查看中文 OpenAPI 文档。

## API 示例

```bash
curl 'http://127.0.0.1:8000/health'
curl 'http://127.0.0.1:8000/api/v1/stocks/600000/history?start_date=2024-01-02&end_date=2024-01-04'
curl 'http://127.0.0.1:8000/api/v1/funds/510300/history?start_date=2024-01-02&end_date=2024-01-04'
```

接口统一返回：

```json
{
  "code": 200,
  "msg": "行情查询成功",
  "data": {
    "asset_type": "stock",
    "symbol": "600000",
    "provider": "mock",
    "mock_data": true,
    "start_date": "2024-01-02",
    "end_date": "2024-01-04",
    "items": []
  }
}
```

代码必须为六位数字，日期采用 `YYYY-MM-DD`，且开始日期不能晚于结束日期。无数据时返回成功和空 `items`。

## 开发检查

```bash
python -m compileall app
python -m pytest
```

接入真实数据源时，实现 `MarketDataProvider` 协议并在 Provider 工厂中注册。Provider 负责获取数据，`MarketDataService` 继续负责过滤、排序、异常映射及响应标准化。
