# 市场快照 V1：本地联调

本说明只用于本地联调。`python -m app serve` 启动服务内定点调度，手动 `collect --force` 仍可使用，但受 Redis 去重和源冷却约束。接口和字段以[市场快照 V1 契约](market-snapshot-v1.md)为准。

## 链路与前置条件

| 环节 | 本地约定 |
| --- | --- |
| 采集 | `be-data-analysis`，Python 3.12，`python -m app serve` 内置调度或手动 `python -m app collect --force` |
| Redis | 同一台本地 Redis，DB 2，字符串键 `stock:market:v1:snapshot`；值是 UTF-8 JSON，不设 TTL |
| 后端 | `be-vita` 的 `vita-admin`，显式使用 `dev` profile，端口 19001、上下文路径 `/admin/api`；`GET /market/dashboard/snapshot` 需要 `market:dashboard:view` 权限 |
| 前端 | `fe-data-stock`，Vite 5173；`/admin/api` 默认代理到 `http://127.0.0.1:19001`，动态路由为 `/market` → `/market/overview` |

`be-vita` 的 `application.yml` 默认激活 **prod**，所以启动本地服务时必须显式传入 `--spring.profiles.active=dev`。先核对 `application-dev.yml` 中的 MySQL、Redis 均指向本地实例；不要以默认 profile 启动，也不要用生产地址或凭据联调。后端的 Redis `spring.data.redis.database` 已配置为 2，采集端的 `REDIS_URL` 也必须以 `/2` 结尾。

本机需要 MySQL 8.x、Redis 7.x、JDK 17、Maven、Python 3.12、pnpm，以及三个仓库的已安装依赖。数据库按 `be-vita/README.md` 初始化本地 `be_vita` 库；在既有升级流程完成后执行 `be-vita/sql/upgrade/20260923_market_dashboard_snapshot.sql`，使行情总览菜单和 `market:dashboard:view` 权限可见。旧菜单清理脚本可能影响现有数据，不为联调单独盲目重跑。登录用户须拥有该权限；更改角色授权后重新登录以刷新权限上下文。

## 手动启动与真实源联调

以下命令分别在终端执行，路径按本机实际检出位置调整。Redis 和 MySQL 只连接本地实例；启动命令因本机安装方式而异。

1. 手动启动 MySQL 和 Redis，确认 `127.0.0.1:3306`、`127.0.0.1:6379` 可连接；确认 Redis DB 2 可读写。启动 `serve` 后内置调度会按交易时段自动采集。
2. 在 `be-vita` 根目录启动管理端：

   ```bash
   mvn -pl vita-admin -am spring-boot:run -Dspring-boot.run.arguments=--spring.profiles.active=dev
   ```

   确认监听 `127.0.0.1:19001`。访问业务接口需要先通过管理端登录并取得 `market:dashboard:view` 权限。
3. 在 `fe-data-stock` 根目录执行 `pnpm dev`，打开 `http://127.0.0.1:5173` 并登录。确认侧栏出现“板块与资金总览”；浏览器网络面板的请求应为 `GET /admin/api/market/dashboard/snapshot`。
4. 在 `be-data-analysis` 根目录，设置仅指向本地 Redis DB 2 的 `REDIS_URL`，手动执行：

   ```bash
   python3.12 -m app collect --force
   ```

   `--force` 只绕过采集时段检查，仍受 Redis 锁、时段占位、全局最小间隔和源冷却约束；命令完成一轮后退出。某个源失败时，快照可能仍被写入，但命令返回非零；检查各模块状态和日志。交易时段可另行手动执行 `python3.12 -m app probe`，只读核对大盘资金流的最新源数据日期。
5. 用 `redis-cli -n 2 GET stock:market:v1:snapshot` 或本地 Redis 工具检查原始 JSON；确认顶层版本 1、三个模块、`generatedAt` 与每模块的 `tradeDate`、`tradeDateBasis`、`status`。在已登录页面核对行业、概念 Top5 和大盘资金流。`FRESH` 不代表源数据为当日实时，特别要比较 `marketFundFlow.tradeDate` 与本地交易日。

## 行情源不可用时的本地快照联调

仅在**专用本地 Redis** 上运行下面的测试数据注入步骤，它会覆盖或删除 DB 2 的快照键。使用 `REDIS_URL=redis://127.0.0.1:6379/2`，若本地实例有密码则在当前终端的环境变量中提供，勿写入仓库。脚本拒绝连接非回环地址及非 DB 2。分别设置 `SNAPSHOT_CASE=normal`、`partial`、`historical` 或 `missing` 并重新运行，随后刷新页面：

```bash
SNAPSHOT_CASE=normal python3.12 - <<'PY'
import json
import os
from datetime import datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import redis

url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/2")
parsed = urlparse(url)
assert parsed.hostname in {"127.0.0.1", "localhost", "::1"} and parsed.path == "/2"
client = redis.Redis.from_url(url, decode_responses=True)
case = os.environ.get("SNAPSHOT_CASE", "normal")
key = "stock:market:v1:snapshot"
if case == "missing":
    client.delete(key)
else:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    date = now.date().isoformat()
    old_date = (now.date() - timedelta(days=1)).isoformat()
    stamp = now.isoformat(timespec="seconds")
    def module(data, basis="CALENDAR", trade_date=date):
        return {"status": "FRESH", "tradeDate": trade_date,
                "tradeDateBasis": basis, "lastSuccessAt": stamp,
                "lastAttemptAt": stamp, "message": None, "data": data}
    def ranking(kind, change, flow):
        return {"sectorName": "示例板块", "sectorType": kind,
                "changePercent": change, "netFlowAmount": flow}
    def top5(kind):
        return {"source": "THS", "period": "INTRADAY",
                "topRise": [ranking(kind, 1.2, 100000000)],
                "topFall": [ranking(kind, -1.2, -100000000)],
                "topInflow": [ranking(kind, 1.2, 100000000)],
                "topOutflow": [ranking(kind, -1.2, -100000000)]}
    def fund_point(day):
        return {"date": day, "mainNetInflow": 100000000,
                "mainNetInflowRatio": 2.5, "superLargeNetInflow": 60000000,
                "superLargeNetInflowRatio": 1.5, "largeNetInflow": 40000000,
                "largeNetInflowRatio": 1.0, "mediumNetInflow": -30000000,
                "mediumNetInflowRatio": -0.75, "smallNetInflow": -70000000,
                "smallNetInflowRatio": -1.75, "shanghaiClose": 3200,
                "shanghaiChangePercent": 0.5, "shenzhenClose": 10500,
                "shenzhenChangePercent": 0.7}
    fund_date = old_date if case == "historical" else date
    point = fund_point(fund_date)
    modules = {
        "industryTop5": module(top5("industry")),
        "conceptTop5": module(top5("concept")),
        "marketFundFlow": module({"latest": point, "series": [point]}, "SOURCE", fund_date),
    }
    if case == "partial":
        modules["conceptTop5"]["status"] = "STALE"
        modules["conceptTop5"]["message"] = "示例：本轮采集失败，保留上次数据"
        modules["marketFundFlow"].update(status="ERROR", tradeDate=None,
                                         lastSuccessAt=None, data=None,
                                         message="示例：首次采集失败")
    assert case in {"normal", "partial", "historical"}
    snapshot = {"schemaVersion": 1, "provider": "akshare", "generatedAt": stamp,
                "modules": modules}
    client.set(key, json.dumps(snapshot, ensure_ascii=False, allow_nan=False))
client.close()
print(f"已写入本地测试场景：{case}")
PY
```

预期：`normal` 显示三个展示区；`partial` 的概念 Top5 显示旧数据提示、大盘资金流显示错误空态；`historical` 显示实际历史日期且不称为当日实时；`missing` 使后端返回业务错误，前端显示页面空态。上述数据均为**合成测试值**，与真实行情无关。测试结束后手动再次采集真实数据，或在专用本地 Redis 上删除该快照键。

## 2026-09-23 本机执行记录

- 静态契约已核对：采集和后端均指向 DB 2、同一快照键；后端路径、权限及 Vite 代理路径一致。
- 手动运行 `.venv/bin/python -m app collect --force`：在获取 Redis 锁阶段失败，无法访问 `localhost:6379`；`nc -vz 127.0.0.1 6379` 明确返回 `Connection refused`。此轮未调用行情源，也未写入快照。
- 本机未找到 `redis-server`、`redis-cli`、Docker、Podman、MySQL 服务端；6379、3306、19001、5173 均无监听。因此未启动后端和前端，也未执行真实 Redis → 后端 → 页面联调。需先提供本地 MySQL/Redis 运行环境，再按上文手动步骤验证。
