# 第三方数据源清单与手动健康验证

## 先区分接口说明、源验证和完整刷新

本说明按项目固定 **AKShare 1.18.97** 的安装源码与当前 Provider 核对，包含 **13 个唯一函数、14 种参数调用组合**。上海字典的主板/科创板是同一函数的两个参数组合；ETF 字典和交易报价共用 `fund_etf_category_sina(symbol="ETF基金")`，不重复计数。一次函数调用可能先建会话、取页数或遍历多页，不能按一次 HTTP 请求估算耗时。

本轮没有真实请求源站，所有源的验证状态均为 **NOT_CHECKED（未检查）**。静态说明保存在 `app/source_catalog.py`，不参与业务路由或采集参数选择。查看说明不会读取 `.env.dev/.env.prod`、创建 Redis 客户端、导入 AKShare 或调用源接口：

```bash
python -m app sources
python -m app sources --json
# 可选：将完整静态元数据复制为本地文件，不是实时健康报告。
python -m app sources --json > /tmp/be-data-analysis-sources.json
```

| 操作 | 实际含义 | 副作用 |
| --- | --- | --- |
| `python -m app sources [--json]` | 输出接口说明 | 无配置读取、Redis 或网络访问 |
| `GET /health` | Redis PING；连接和读取各一秒，不重试；不可用 HTTP 503 | 不调用行情源，不能证明行情源健康 |
| 手动单源调用 Provider + 标准化 | 检查某源请求/字段/业务可用性 | 有真实源请求/会话，并写共享配额、速率、适用冷却控制键；不运行采集器、不写业务快照/曲线/事件 |
| `collect/calendar-refresh/monitor-sample/etf-collect` | 完整业务流程 | 会写 Redis 锁、限频、日历、快照或曲线并可能发布更新 |
| scheduler 固定 POST 刷新 | Java 与 Python 完整同步 | 可写 MySQL、Redis、锁和缓存，不能作为只读源健康检查 |

当前 CLI 没有 `probe` 子命令。验证单个源应使用下述 Provider/标准化组合；`collect --force` 仍受交易日、时段、锁、间隔与冷却约束，不能用它绕过这些条件进行探测。

## 实际请求域名

以下为当前生产调用实际访问的域名，供代理过滤核对；上交所 `www.sse.com.cn` 只出现在 Referer 中，不应误计为该字典的数据请求域名。当前清单没有东方财富调用。

```text
finance.sina.com.cn
vip.stock.finance.sina.com.cn
data.10jqka.com.cn
query.sse.com.cn
www.szse.cn
www.bse.cn
xueqiu.com
stock.xueqiu.com
fund.10jqka.com.cn
danjuanfunds.com
```

请求方法、协议、主要路径、关键 HTTP 参数及来源 Provider 的完整机器说明见 `python -m app sources --json`。这些是当前安装版本的实现路径，不是稳定的源站契约；HTTP 200、首页正常或浏览器可打开，都不等于业务数据有效。需要诊断代理影响时，可在独立手动验证进程中清空代理变量/设置 `NO_PROXY`，只运行一次；不要修改生产全局配置或通过重复请求消耗限频。

## 逐源用途、参数与业务判定

| 调用组合 | Provider / 标准化入口 | 主要请求路径 | 业务可用判定与语义 |
| --- | --- | --- | --- |
| `tool_trade_date_hist_sina()` | `AkShareCalendarSource.dates()` / `normalize_dates()` | `finance.sina.com.cn/realstock/company/klc_td_sh.txt`，GET | `trade_date` 每项有效，上海当年日期非空；缓存范围必须覆盖待判断日期。失败保留旧缓存，旧年不能用于新年，不按工作日填造交易日。 |
| `stock_fund_flow_industry(symbol="即时")` | `AkShareMarketProvider.sector_fund_flow("industry")` / `normalize_sectors()` | `data.10jqka.com.cn/funds/hyzjl/`，取页数及分页 GET | 必需行业/指数/涨跌幅/流入/流出/净额列；排除无名、重名及全关键值无效行，至少一条有效板块。 |
| `stock_fund_flow_concept(symbol="即时")` | `sector_fund_flow("concept")` / `normalize_sectors()` | 同域 `/funds/gnzjl/`，分页 GET | 输出列仍叫“行业”，按相同板块规则标准化，不能因列名将概念误判成行业。 |
| `stock_fund_flow_individual(symbol="即时")` | `market_fund_flow()` / `normalize_individual_batch()` | 同域 `/funds/ggzjl/`，分页 GET | 六位代码、名称有效，涨跌幅/流入/流出必需有效；有效重复行冲突或关键值缺失整批拒绝。全市场与启用个股资金点共用此批次，不加逐股调用。 |
| `stock_zh_index_spot_sina()` | `index_spot()` / `normalize_core_indices()` | `vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeStockCountSimple`、`getHQNodeDataSimple`，`node=hs_s` | `sh000001/sz399001/sh000300/sz399006/sh000688` 全部存在、点位>0、重复无冲突；缺任一整个指数模块降级。 |
| `stock_info_sh_name_code(symbol="主板A股")` | `ExchangeStockProvider.all_a_stocks()` | `query.sse.com.cn/sseQuery/commonQuery.do`，GET，`STOCK_TYPE=1` | 主板组合非空，证券代码/证券简称有效，六位代码。Provider 同步前清 `lru_cache`。 |
| `stock_info_sh_name_code(symbol="科创板")` | 同上 | 同一路径，`STOCK_TYPE=8` | 科创板组合非空，合并后同代码不能冲突；市场仍为 SH。 |
| `stock_info_sz_name_code(symbol="A股列表")` | 同上 | `www.szse.cn/api/report/ShowReport`，GET，`SHOWTYPE=xlsx,CATALOGID=1110,TABKEY=tab1` | 有效非空 Excel，A股代码/A股简称有效。四组字典合并后 SH/SZ/BJ 均须有股票。 |
| `stock_info_bj_name_code()` | 同上 | `www.bse.cn/nqxxController/nqxxCnzq.do`，POST，`page` 从0开始 | 获取页数再分页，证券代码/证券简称有效；不能仅凭首页 HTTP 成功判定整个北京清单有效。 |
| `stock_individual_basic_info_xq(symbol,token,timeout)` | `XueqiuProvider.profile()` / `normalize_profile()` | 先 GET `xueqiu.com/` 建会话，再 `stock.xueqiu.com/v5/stock/f10/cn/company.json?symbol=...` | 非空 `item/value`；行业来自 `affiliate_industry.ind_name`，上市日期来自 `listed_date`。缺市值会另调报价，缺失字段保持空。 |
| `stock_individual_spot_xq(symbol,token,timeout)` | `XueqiuProvider.quote()` / `normalize_quote()` | 先建雪球会话，再 `/v5/stock/quote.json?symbol=...&extend=detail` | `symbol=SH/SZ/BJ+六位代码`；有效源时间，现价/涨幅/成交额至少一项有效。历史值能解析不等于实时；未来日期、旧日期、时间倒退/不变按业务拒绝或降级。 |
| `fund_etf_category_sina(symbol="ETF基金")` | `AkShareEtfProvider.quotes()` / `catalog()`、`quote_rows()` | 新浪同域 `/quotes_service/api/jsonp.php/IO.XSRV2.CallbackList['da_yPT46_Ll7K6WD']/Market_Center.getHQNodeDataSimple`，`node=etf_hq_fund,page=1,num=5000` | 字典需要有效 sh/sz 六位代码及名称；报价需要>0交易价、重复无冲突。字典与报价的判定不同；监控代码缺失降级。固定版本当前只取这一页，不能把可返回记录数解释为源站完整性保证。 |
| `fund_info_ths(symbol="六位代码")` | `AkShareEtfProvider.profile()` / `ths_profile()` | `fund.10jqka.com.cn/{code}/interduce.html`，GET；禁止重定向 | `字段/值` 非空且不冲突，基金代码匹配；全称、基金类型、投资类型、经理、成立日期、业绩基准、管理人、托管人八项至少一项有效。 |
| `fund_individual_detail_hold_xq(symbol,date,timeout)` | `asset_allocation()` Provider / 同名标准化函数 | `danjuanfunds.com/djapi/fundx/base/fund/record/asset/percent`，GET，`fund_code=六位代码,report_date=YYYY-MM-DD` | AKShare 的 `date=YYYYMMDD`。至少一个有效资产类型及有限的0–100仓位占比；服务要求雪球总闸和已配置Token，但这个函数**没有 token 形参，不会传 Token 给它**。 |

### 时间、单位与认证前提

- 所有验证记录采用上海时间 `+08:00`，区分采集时刻、源交易时间和请求报告期。
- 同花顺板块纯数值资金按**亿元**转元；同花顺个股纯数值资金为**元**。显式元/万/亿优先。个股有效批次的 `netAmount=流入-流出`，可缺源净额仅用于同一有效子集审计，不得因未审计股票导致误报差额。板块净流率为 `净额/(流入+流出)*100`，涨跌幅与仓位占比均为百分数。
- 同花顺没有可靠源日期/逐条源时间；`tradeDate` 来自日历，`collectedAt/lastSuccessAt` 是采集时刻。新浪指数也无可靠逐条源时间，不用采集时刻证明源实时或已收盘。
- 雪球个股有真实源时间；固定版 AKShare 将毫秒时间戳转成无时区字符串，Provider 设置上海时区。价格元、成交额/市值元，价格曲线按源时间写入。同日源时间须**严格晚于15:00**才可确认收盘；15:00整、采集完成时间或历史数据都不能替代该条件。
- 新浪 ETF 价格是**交易价而非净值**，`sourceTime=null`，完成采集时间不代表收盘确认。当前没有可靠 ETF 资金流，`fundSeries=[]`、`fundFlowStatus=NO_RELIABLE_SOURCE`，不得填造资金流。
- 同花顺 ETF 成立日期≠上市日期、基金经理≠基金管理人，业绩基准不用于推导跟踪指数。雪球/蛋卷资产配置是资产类别占比，不是成分股持仓；`requestedReportPeriod` 只是请求期，不能当作真实披露日期。
- 新浪/同花顺/交易所源不需要本服务的雪球 Token。雪球个股和 ETF 资产配置须先确认 `STOCK_MONITOR_XQ_ENABLED=true` 且 `XUEQIU_TOKEN` 已配置；总闸默认关闭。手动验证也遵守授权前提，日志只写“已配置/未配置”，不得打印 Token、Cookie、密码或原始响应。

## 频率、超时与冷却

HTTP 连接上限5秒、读取默认15秒，由 `SOURCE_TIMEOUT_SECONDS` 控制读取上限。已有更短 scalar/tuple 超时保持不变，缺失/None 补齐。HTTP read 上限与**整次函数/批次预算**不同。

| 业务 | 调用约束 |
| --- | --- |
| 市场行业/概念/个股/指数 | 整次源预算分别120/120/900/120秒；行情120秒间隔，慢任务等完成后跳过错过时段，不补跑、不重叠；THS分页>=1秒，新浪指数连续请求>=0.2秒。 |
| 日历 | 子进程取结果等待 `SOURCE_TIMEOUT_SECONDS+10`（默认25秒），随后 join 最多2秒，超时终止回收；每月1日00:10、自动失败每日限频，手动600秒限频。 |
| 交易所字典 | 四组受控并发，全部成功并校验后合并；每次调用60秒（含等待/回收），每次HTTP>=1秒，清理缓存。 |
| 雪球个股 | 同股报价TTL=120秒，跨股票受控并发；行情批次300秒、资料批次600秒、单次源300秒，会话及数据每次HTTP>=1秒，不能仅按一次read估算完整资料批次。 |
| 新浪 ETF | 子进程总预算 `SOURCE_TIMEOUT_SECONDS+17`（默认32秒），预留2秒回收；SINA组跨入口实际HTTP>=0.2秒，行情120秒。固定函数目前一次 GET。 |
| 同花顺 ETF 资料 | 同代码30分钟限频、最多10只、并发最多2路；批次180秒，源请求>=2秒，单次子进程不超过剩余批次预算；Python锁210秒，Java等待210秒。已发起请求的失败受既有资料限频保护；预算/配额耗尽且没有HTTP则SKIPPED并释放该次预留。 |
| ETF 资产配置 | 单次子进程默认32秒；内部接口独立调用，不属于八个固定刷新入口，不使用 ETF 行情冷却键。 |

市场 HTTP403/429、网络/超时为现有源7200秒保护；THS 的既有 `AttributeError/IndexError` 也保护7200秒。市场 `SourceDataError` 只将失败模块冷却300秒。ETF**行情**403/429为7200秒，普通网络/超时/格式/标准化错误300秒；不把行情冷却规则误套到字典或资料路由。股票雪球认证拒绝/限流沿用7200秒保护。冷却跳过只记录INFO及剩余TTL，不访问源、不续期、不增加曲线点，保留旧有效数据为STALE。

直接调用Provider也需要Redis控制层，会写配额、HTTP速率和适用冷却键，但不写业务快照、曲线或事件。验证人员须使用正确环境的控制Redis，并确认保护状态，选一个源串行一次、不并行分页、不立即重复重试。未检查保护信息时记SKIPPED。

## 共享执行控制与及时发布

- CLI、调度、同步内部API和直接Provider共用 `stock:source-control:v1:` 控制键。全局4个 `slot:global:{0..3}`，每组2个 `slot:{ths|sina|xq|sse|szse|bse}:{0..1}`；XQ含蛋卷。同花顺行业/概念/个股/基金资料共享THS，日历/指数/ETF共享SINA。
- 租约令牌TTL30秒，每10秒续租；未回收子进程不得释放名额，旧令牌不得删除新持有者。`rate:{group}` 和 `rate:ths:fund` 为HTTP启动时间门槛，TTL30秒；雪球采样沿用同代码120秒键。资料市值补充不套用采样同股间隔。
- 每次函数调用独立spawn，不再嵌套ETF/日历真实源进程。子进程只请求源及操作控制键；父进程负责标准化、合并和业务写入。所有会话首页、分页、数据请求及重定向都先检查配额、业务锁、冷却和HTTP速率许可。分页串行；失败不继续下一页。
- 每批只保留有限候选、不排队补跑旧轮。等待/初始化/HTTP/TERM→KILL→JOIN回收均计入原总预算，清理最多2秒。取消、控制Redis失败或业务锁失效停止调用；源进程看门狗在父进程消失时退出，禁止旧轮回写。
- 市场优先准入市场资金与行业；概念等THS空位，指数独立SINA。每完成一模块，父进程事务合并完整快照并更新版本/通知；未完成模块保持旧数据与时间。市场成功仅追加一次市场/个股资金点，指数仅追加一次；结束时不重复发布。
- 个股最多10只受控并发，每股完成立即发布，曲线仍用源时间；资料跨股并发、同股先资料后可选市值补充，整批成功后按请求顺序返回。股票资料及采样继续共用锁。ETF清单仍只调一次新浪全表；ETF资料按代码记录OK/ERROR/SKIPPED并按配置顺序返回；资产配置只占一个共享名额。
- V1业务键、HTTP响应、同步完成语义、GET/SSE、严格晚于15:00的收盘确认、两日历史及雪球总闸保持原约束，不新增jobId或队列。

## 只读业务数据的单源验证步骤

1. 确认运行版本与部署环境。`APP_ENV=prod` 对应生产，默认 `.env.dev` 对应本地；仅说明命令无需这些配置。确认要验证的函数、参数组合与授权条件，避免误调默认 `LOF基金` 或非即时资金接口。
2. 选择清单里的 Provider 方法；在独立入口进程执行，Provider统一管理spawn源进程；控制配额与实际HTTP限频同样生效。**不要实例化采集器、调用工作流、启动 `serve` 或 POST 刷新接口**，它们会有缓存/调度副作用。
3. 将返回结果送入对应纯标准化函数。记录原始结果行数、字段名称是否符合校验、标准化有效行数；不输出原始行、响应正文或凭据。交易所 Provider 本身已合并字典；额外源调用需按模板明确指定参数。
4. 分别判断传输、解析、业务可用与时间新鲜度。HTTP成功不代表字段有效；字段有效不代表当天实时。新浪/THS没有可靠源时间时记录“源时间不可用”，不能写成实时健康。
5. 记录上海时间、脱敏函数参数、耗时、行数、字段校验、数据日期/源时间、结果状态与异常类别/HTTP状态。`DISABLED/SKIPPED/COOLDOWN/NOT_CHECKED` 不误报为源失败；普通网络错误、HTTP拒绝、格式异常和业务标准化异常分别记录。一次失败停止验证，不即时重试。

下面是**供人工执行**的行业单源示例；本轮没有运行此示例。它调用真实源并按业务标准化，需要读取当前环境Redis配置，并写控制键，不写业务缓存。请先确认生产最近请求、间隔和冷却状态：

```bash
# 在 be-data-analysis 根目录，用已安装运行依赖的 Python 执行。
python -c '
import json, time
from datetime import datetime
from zoneinfo import ZoneInfo
from app.providers.akshare_market import AkShareMarketProvider
from app.providers.http import error_metadata
from app.normalize import SourceDataError, normalize_sectors
started = time.monotonic()
record = {"checkedAt": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
          "function": "stock_fund_flow_industry", "parameters": {"symbol": "即时"}}
try:
    rows = AkShareMarketProvider(15).sector_fund_flow("industry")
    result = normalize_sectors(rows, "industry")
    record.update(status="VALID_DATA", rowCount=len(rows),
                  validCount=len(result["items"]), fieldValidation="PASS",
                  sourceTime=None, freshness="SOURCE_TIME_UNAVAILABLE")
except Exception as exc:
    record.update(status=({"SourceCoolingError": "COOLDOWN", "SourceNotStartedError": "SKIPPED",
                           "SourceThrottledError": "SKIPPED"}.get(type(exc).__name__, "FAILED")),
                  **error_metadata(exc))
    if isinstance(exc, SourceDataError):
        record.update(reason=exc.reason, fields=exc.fields,
                      code=exc.code, badRows=exc.bad_rows)
record["elapsedSeconds"] = round(time.monotonic() - started, 2)
print(json.dumps(record, ensure_ascii=False))
'
```

其他源按上表替换 Provider 与标准化入口。ETF `profile()` 必须提供 `budget_seconds`；直接验证一次不能超过资料批次剩余预算。验证股票雪球时先检查总闸/Token，使用 `XueqiuProvider` 传入授权值但不输出；ETF资产配置只传 `symbol/date/timeout`，不加不存在的 `token` 形参。

记录模板（不是当前健康结果）：

```json
{
  "checkedAt": "<上海时间+08:00>",
  "function": "<函数>",
  "parameters": {"symbol": "<脱敏后参数>"},
  "elapsedSeconds": null,
  "rowCount": null,
  "fieldValidation": "NOT_CHECKED",
  "sourceTime": null,
  "dataDate": null,
  "status": "NOT_CHECKED",
  "httpStatus": null,
  "exceptionType": null,
  "rootType": null,
  "category": null
}
```

## 完整刷新入口（有落库/缓存副作用）

在 **scheduler 所在宿主机**真实回环直连。生产端口19004、开发19005，前缀 `/scheduler/api/local/market-data/v1`；不加转发头、不通过网关。Java本机入口无需登录/令牌，Java调用Python仍保留 `X-Internal-Token`。本机直连规则不能用于跳过Python内部鉴权。

| scheduler POST 后缀 | Python 内部 POST | 刷新结果/副作用 |
| --- | --- | --- |
| `/stock/dictionary/refresh` | `/internal/stock-monitor/v1/exchange-dictionary` | Java持久化股票字典，可能同步缓存。 |
| `/stock/profiles/refresh` | `/internal/stock-monitor/v1/profiles` | Java持久化资料；Python会写Redis锁并检查共享冷却，必要时额外请求报价。 |
| `/calendar/refresh` | `/internal/jobs/v1/calendar/refresh` | Redis当年日历、刷新锁与限频；任务等待上限60秒。 |
| `/market/refresh` | `/internal/jobs/v1/market/refresh` | Redis市场快照、启用个股资金点、锁/限频/冷却及更新通知；1380秒。 |
| `/stock/quotes/refresh` | `/internal/jobs/v1/monitor/refresh` | Redis报价/曲线/状态及通知；受雪球闸；300秒。 |
| `/etf/dictionary/refresh` | `/internal/etf-monitor/v1/dictionary` | Python返回字典数据包，Java完成持久化；Java等待60秒。 |
| `/etf/profiles/refresh` | `/internal/etf-monitor/v1/profiles` | 同花顺资料返回包，Python写独立锁和30分钟TTL，Java持久化；Java等待210秒。 |
| `/etf/quotes/refresh` | `/internal/jobs/v1/etf/refresh` | Redis ETF快照/交易价格曲线/状态及通知；120秒。 |

固定本机入口不需要请求体，由Java选择当前监控清单。Python资料入口只接收 `symbols`（最多10个），ETF资料不接收 `asOfDate`。字典/资料返回数据包，由Java持久化；四种任务返回同步终态包，不创建任务号。ETF资产配置是单独的 `/internal/etf-monitor/v1/asset-allocation`，请求 `{"symbol":"SH510050","reportPeriod":"YYYYMMDD"}`，不属于这八项。

完整刷新示例（**会写业务数据**，只在确实需要刷新时执行；市场等待高于1380秒）：

```bash
curl --noproxy '*' --proxy '' --max-time 1440 -X POST \
  http://127.0.0.1:19004/scheduler/api/local/market-data/v1/market/refresh
# 开发环境将19004改为19005；其它业务使用上表对应后缀。
```

手动行情刷新仍受交易日、时段、锁、120秒间隔和冷却限制，`SKIPPED/DISABLED` 不证明源失败。检查返回的业务状态与模块快照，不把HTTP200误读为所有源成功。详细同步结果、锁和等待限制见 [交易日历与内部任务V1](trading-calendar-jobs-v1.md)。
