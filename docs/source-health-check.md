# 第三方数据源清单与手动健康验证

## 先区分接口说明、源验证和完整刷新

本说明按项目固定 **AKShare 1.18.97** 的安装源码与当前 Provider 核对，包含 **13 个唯一函数、14 种参数调用组合**。上海字典的主板/科创板是同一函数的两个参数组合；ETF 字典和交易报价共用 `fund_etf_category_sina(symbol="ETF基金")`，不重复计数。一次函数调用可能先建会话、取页数或遍历多页，不能按一次 HTTP 请求估算耗时。

本轮没有真实请求源站，所有源的验证状态均为 **NOT_CHECKED（未检查）**。静态说明保存在 `app/providers/catalog.py`，不参与业务路由或采集参数选择。查看说明不会读取 `.env.dev/.env.prod`、创建 Redis 客户端、导入 AKShare 或调用源接口：

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

### 完整业务依赖与复用落点

下表与上面的真实域名/路径表逐项对应，共13函数/14参数组合。行情和曲线只写Redis；Python字典、资料、资产配置返回包由Java落MySQL。管理页面的字典/资料刷新均经Java受保护内部入口；三块业务屏为市场总览、股票监控、ETF监控。

| AKShare函数＋参数 | 真实源／标准化输出 | 调用入口与周期 | Redis／Java MySQL | 管理端、业务屏与复用／授权 |
| --- | --- | --- | --- | --- |
| `tool_trade_date_hist_sina()` | 新浪finance；当年交易日期列表 | 启动补建、月初00:10、calendar-refresh／jobs calendar | `stock:calendar:v1:trading-days`；不落行情库 | 三屏采集共用同一日历；公开源，总闸无关 |
| `stock_fund_flow_industry(symbol="即时")` | 同花顺data；行业名称、指数、涨幅、流入/流出/净额元 | 行情通道指数后；collect／jobs market，启动≥120秒 | `stock:market:v1:snapshot.modules.industrySectors`及原更新频道 | 市场总览行业热力图/涨跌Top10/资金Top10复用一批；公开源 |
| `stock_fund_flow_concept(symbol="即时")` | 同花顺data；同上，概念维度 | 行情通道行业后；collect／jobs market | 同快照`conceptSectors` | 市场总览概念热力图/涨跌Top10/资金Top10复用一批；公开源 |
| `stock_fund_flow_individual(symbol="即时")` | 同花顺data；全市场流入/流出/净额、涨跌计数、逐股资金点 | 独立资金通道；collect／jobs market，稳定代码倒序分页 | 同快照`marketFundFlow`＋`stock:monitor:v1:fund-series:{date}:{symbol}`、state-id/原通知同事务 | 总览资金/宽度＋启用≤10股票资金曲线复用全市场一批；Java由模块/日期/点生成AVAILABLE/STALE/NO_DATA/DISABLED；公开源 |
| `stock_zh_index_spot_sina()` | 新浪vip；五核心指数价格/涨幅、指数曲线 | 行情通道ETF后；collect／jobs market | 同快照`coreIndices`及模块内曲线 | 总览指数卡及曲线同批；公开源 |
| `stock_info_sh_name_code(symbol="主板A股")` | SSE query；symbol/code/name/market=SH | Python stock exchange-dictionary按需；Java工作日16:30整体刷新/手动刷新 | 返回→`stock_symbol_dictionary` | 股票字典管理→股票监控选股；与其他3组合串行合并；公开源 |
| `stock_info_sh_name_code(symbol="科创板")` | SSE query；同上SH，非第二个唯一函数 | 同上 | 同上 | 同上 |
| `stock_info_sz_name_code(symbol="A股列表")` | SZSE；同上market=SZ | 同上 | 同上 | 同上，全部组合成功才返回 |
| `stock_info_bj_name_code()` | BSE分页；同上market=BJ | 同上 | 同上 | 同上，原POST分页串行 |
| `stock_individual_basic_info_xq(symbol,token,timeout)` | 雪球stock；industry/listingDate/marketCap可空 | Python stock profiles按需；Java工作日16:30整体刷新/手动资料刷新 | 返回→`stock_monitor_profile` | 股票资料管理→股票监控基础资料；资料仍查原源，仅缺市值时复用同代码XQ/FRESH且120秒内报价市值；总闸＋授权Token |
| `stock_individual_spot_xq(symbol,token,timeout)` | 雪球会话＋quote；现价/涨幅/量额/源时间/市值元 | 行情通道首；monitor-sample／jobs monitor；资料缺市值时补；同股120秒 | `stock:monitor:v1:quote:{symbol}`＋price-series/state-id/原频道 | 股票监控报价及价格曲线，市值供资料复用；源时间收盘严格>15:00；总闸＋授权Token |
| `fund_etf_category_sina(symbol="ETF基金")` | 新浪vip；ETF代码/名称、交易价/量额 | 行情通道第二；etf-collect／jobs etf；Python dictionary按需优先当日缓存，缺失一次；Java工作日16:40整体刷新/手动字典刷新 | `stock:etf-monitor:v1:snapshot`、price-series及dictionary-source(TTL86400)；字典返回→`etf_symbol_dictionary` | ETF字典管理及ETF监控复用全表；非启用子集、非基金净值；公开源，总闸无关 |
| `fund_info_ths(symbol=六位代码)` | 同花顺fund；八项基本资料至少一项有效 | Python etf profiles按需；Java工作日16:40整体刷新/手动资料刷新；同代码30分钟、≤10只串行、180秒批次 | 返回→`etf_monitor_profile`；控制锁/限频Redis，不保存原表 | ETF资料管理→ETF监控资料；成立日不作上市日；公开源，总闸无关 |
| `fund_individual_detail_hold_xq(symbol,date,timeout)` | 蛋卷；资产类别/仓位百分比，请求期不作披露日 | 独立etf asset-allocation；只查指定报告期 | 返回→`etf_asset_allocation_report` | ETF资料/监控资产配置；公开状态由Java按MySQL报告＋总闸生成；服务要求总闸/已配Token，原函数无token参数 |

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
| 市场行业/概念/个股/指数 | 整次源预算分别120/120/900/120秒；行情120秒间隔，快轮按未来定点节奏；慢轮耗时>=120秒完成回收后窗口内立即接续，不补旧轮、不重叠；THS分页>=1秒，新浪指数连续请求>=0.2秒。 |
| 日历 | 子进程取结果等待 `SOURCE_TIMEOUT_SECONDS+10`（默认25秒），随后 join 最多2秒，超时终止回收；每月1日00:10、自动失败每日限频，手动600秒限频。 |
| 交易所字典 | 四组串行，全部成功并校验后合并；每次调用60秒（含等待/回收），每次HTTP>=1秒，清理缓存。 |
| 雪球个股 | 同股报价TTL=120秒，跨股票串行；行情批次300秒、资料批次600秒、单次源300秒，会话及数据每次HTTP>=1秒，不能仅按一次read估算完整资料批次。 |
| 新浪 ETF | 子进程总预算 `SOURCE_TIMEOUT_SECONDS+17`（默认32秒），预留2秒回收；SINA组跨入口实际HTTP>=0.2秒，行情120秒。固定函数目前一次 GET。 |
| 同花顺 ETF 资料 | 同代码30分钟限频、最多10只、串行1路；批次180秒，源请求>=2秒，单次子进程不超过剩余批次预算；Python锁210秒，Java等待210秒。已发起请求的失败受既有资料限频保护；预算/配额耗尽且没有HTTP则SKIPPED并释放该次预留。 |
| ETF 资产配置 | 单次子进程默认32秒；内部接口独立调用，不属于八个固定刷新入口，不使用 ETF 行情冷却键。 |

市场HTTP403/429或明确风控拒绝共享源暂停7200秒；普通网络/读取超时、`AttributeError/IndexError`等解析失败及`SourceDataError`只暂停失败模块300秒。父进程与源子进程使用同一分类，按同一键NX写入，不扩大冷却范围、不续期。ETF**行情**403/429为7200秒，普通网络/超时/格式/标准化错误300秒；不把行情冷却规则误套到字典或资料路由。股票雪球认证拒绝/限流沿用7200秒保护。冷却跳过只记录INFO及剩余TTL，不访问源、不续期、不增加曲线点，保留旧有效数据为STALE。

直接调用Provider也需要Redis控制层，会写配额、HTTP速率和适用冷却键，但不写业务快照、曲线或事件。验证人员须使用正确环境的控制Redis，并确认保护状态，选一个源串行一次、不并行分页、不立即重复重试。未检查保护信息时记SKIPPED。

## 双通道执行控制与及时发布

- 一个服务父进程、两条固定自动调度循环，各有独立执行器、线程上下文、guard/deadline/cancelled；资金只取`stock_fund_flow_individual(symbol="即时")`，行情顺序股票报价→ETF→核心指数→行业→概念。同通道串行、启动间隔≥120秒；跨通道并行，慢资金完成回收后按当前窗口接续，不阻塞行情、不重叠、不补历史点。普通模块失败只降级自身；Redis控制失败或资源不足停止调用。
- 固定入口`stock:source-control:v1:entry:quotes`、`:entry:funds`。CLI、内部API和直接Provider均受控，SourceCall仅将即时individual认作资金。资料/字典/日历/资产配置走quotes；同步完整market原子取得两个，否则立即locked/409且不留半把锁。令牌TTL30秒，每10秒续租；旧令牌不能删除新持有者。GET/SSE不取采集入口。
- 全局`slot:global:{0,1}`及每组`slot:{ths|sina|xq|sse|szse|bse}:{0,1}`各最多2，XQ含蛋卷。源进程回收后按令牌释放；子进程仅请求源/控制键，并转records释放DataFrame；服务父进程不导入AKShare/pandas。Session.send补丁仅在spawn源进程；会话/分页/重定向仍按原HTTP间隔、域名、源超时及冷却规则，分页串行、失败不继续后页。
- 日历仍由quotes入口启动补建/月度循环维护。**资金只读当年合法、覆盖该日的缓存**；缺失/覆盖不足为UNKNOWN，只降级资金模块、不发日历HTTP、不跨占quotes入口、不消费日历AUTO_RETRY_KEY。缓存已覆盖但月刷新到期时，资金仍按有效日期判断，由行情/月度入口刷新。工作日不等同交易日，UNKNOWN/休市不请求业务源。
- 每个行情模块只发布自身patch，不初始化或降级资金；资金只发布marketFundFlow，不修改指数/行业/概念。WATCH读取最新快照，再MULTI/EXEC合并本次模块；最多3次冲突重试（首次＋3次，共4次），每次重读base/version。冲突耗尽明确失败，保留其他已发布数据；模块各自时间保持真实，快照generatedAt不倒退。版本/市场通知同事务，连续previousSnapshotId链支持原REST全量＋SSE增量/缺口重同步。
- 同一全市场资金批次生成汇总、宽度及启用≤10股票资金点，不逐股加请求。资金点只随成功发布一次；原monitor_event_lock序列化资金/报价的股票stateId事务。无新点但真实资金结果/日期/诊断变化仍通知当前启用股票；重试时间单独变化不重复发通知，不造点、不将报价/曲线落MySQL。
- 股票资料仍调用原资料源；**只复用市值**：同代码、XQ、FRESH、collectedAt不超过120秒的有限有效值。删除当前报价生产者无法提供industry/listingDate的全缓存资料分支；缺市值补报价仍受同股120秒。Python无独立定期资料轮询；Java上海工作日16:30股票/16:40ETF整体刷新不改，工作日不保证交易。
- 新浪ETF全表一次取回生成启用交易报价＋`dictionary-source`轻量全量字典，TTL86400，不永久保留原表、不缓存启用子集。字典优先合法上海当日缓存，否则一次源调用；写缓存失败属Redis故障而非源格式/冷却。ETF交易价不换净值；fundFlowStatus=NO_RELIABLE_SOURCE。资产配置状态仍由Java按MySQL报告/总闸生成，Python行情不推测报告状态。
- Compose内存与memory+swap均1g（无额外swap），pids128/init及BLAS线程1保留。cgroup总内存≥800MiB停止准入并TERM/KILL/reap当前源；两源分别取消回收。RESOURCE不触发源冷却，Redis失败独立终止。日志记录init/request/serialization耗时、退出码/信号/取消原因、current/peak及可取得的oom_kill；SIGKILL不能直接认定OOM。
- 资产配置固定detail.reason=RESOURCE/NO_DATA/DISABLED/SOURCE，忙409、冷却/间隔429、参数422、资源/关闭503、源/无数据502。NO_DATA仅真实空/指定期无有效类别；格式变化/未知错误SOURCE。不扫其他期、不重试。公开路径/schemaVersion/业务键及严格源时间>15:00收盘、15:02/04/06/08/10补收盘、雪球总闸不变。
- 分包：api路由；runtime跨域工作流/配额/源执行/资源/双通道调度；providers源HTTP/THS分页/catalog；calendar、market、stock_monitor、etf_monitor各归领域。删除unused JOB_TIMEOUT_SECONDS、completed的忽略limit参数及全缓存资料分支，没有legacy别名或新jobId。Python没有HTML渲染点，JSON/SSE保持结构化原文，不做所有字符串Filter/全局escape；实际展示和HTTP/HTTPS外链由渲染端防护，业务库不存转义文本。

## 即时个股分页修复与验证限制

固定AKShare1.18.97的即时个股函数先按code倒序取页数、随后原源码按zdf分页。涨幅排序在长采集过程中会移动，最新日志2026-10-09 15:04:06/15:12:10已记录DUPLICATE_CONFLICT（74/134行，公开代码300613/605133），此前批次未发布资金点。排序移动是根据源码与重复证据作出的推断，未以真实源复测证明。

现仅对`stock_fund_flow_individual(symbol="即时")`、`data.10jqka.com.cn`准确`/funds/ggzjl/field/zdf/order/desc/page/{n}/ajax/1/free/1/`改为`field/code`；初始取页数请求不变，金额仍由原AKShare解析。行业/概念和3/5/10/20日参数不改。审计初始总页数、页序、非空页面、六位代码严格倒序、全批无重复及原始行数；分页响应若带page_info则核对，无该字段时按初始页数及请求顺序审计。源不支持稳定代码排序或出现缺页/重复/冲突时拒绝全批；不做最后值覆盖和不完整汇总。关键金额与有限聚合校验保持严格。

离线mock固定AKShare真实解析覆盖多页/单位、精确改写范围、重复/排序/缺页/行数、关键金额以及资源/事件/缓存/调用次数。**本机没有Docker，1GiB受限Linux容器实测未执行，真实单轮未执行**；生产日志未提供OOM计数/证据，不宣称早退是OOM。后续仅在非生产、1GiB受限容器中最多单轮验证，不接生产Redis或Token，失败停止；记录cgroup总峰值、函数耗时、行数、业务校验及退出原因，不把离线通过等同源当前可用。

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
from app.market.normalize import SourceDataError, normalize_sectors
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

## 2026-10-09 09:32–09:36 日志核对（只读）

已收到的生产日志只能确认：`collect/etf-collect throttled` 表示120秒最小请求间隔保护返回，不表示完成采集；锁竞争会返回`locked`；三个THS模块以0.00秒及`SourceCooldownError`退出说明被既有冷却跳过；新浪指数成功仅证明该指数调用当时成功。无法据此判断首次触发的是403/429、网络超时还是解析/校验问题，也不能从ETF的一个`RuntimeError`推断底层原因。

| 核对点 | 前轮受控实现及本轮实现应有的特征 | 新生产日志特征 |
| --- | --- | --- |
| 冷却异常 | `SourceCoolingError`；INFO记录剩余TTL | `SourceCooldownError`及失败日志 |
| Provider异常诊断 | 类型、底层异常、HTTP状态、分类 | 仅异常名、0.00秒 |
| 日志上下文 | 上海时间、PID、task | 应核对完整行是否具备这些字段 |
| 分页进度 | 源worker内`quiet_progress()`关闭tqdm | 仍出现0/8至8/8 |
| ETF未知异常 | 脱敏类别与保留原调用帧的诊断 | 仅`RuntimeError`不足以定位 |

这些特征提示运行文件、解释器/目录、容器实例或日志来源需要核对，**不能仅凭片段断言旧镜像**。新2/2双通道配置尚未部署时，生产仍为前轮配额本身正常；异常名、脱敏日志和关闭进度条则是前轮已有特征。挂载的日志文件跨容器重建保留旧记录，需要匹配时间、PID、task及镜像ID，不将混合记录误当当前进程输出。

### 1. 核对容器与实际运行文件

以下命令仅供在生产项目目录人工执行，不执行刷新或请求数据源，也不输出容器环境变量。不要用完整`docker inspect`、`env`或`cat .env.prod`回传结果。

```bash
docker compose ps be-data-analysis
docker inspect --format '{{.Image}} {{.State.StartedAt}}' "$(docker compose ps -q be-data-analysis)"
docker compose exec -T be-data-analysis python - <<'PY'
import hashlib, importlib.util, json, os, re, sys
from importlib.metadata import version, PackageNotFoundError
from pathlib import Path

def link(path):
    try:
        return str(Path(path).resolve(strict=True))
    except OSError:
        return None

spec = importlib.util.find_spec("app")
root = Path(spec.origin).parent if spec and spec.origin else None
try:
    ak_version = version("akshare")
except PackageNotFoundError:
    ak_version = "NOT_INSTALLED"
files = {}
for name in ("runtime/source_execution.py", "market/collector.py", "providers/akshare_market.py",
             "providers/http.py", "etf_monitor/collector.py", "runtime/scheduler.py"):
    path = root / name if root else None
    if not path or not path.is_file():
        files[name] = {"present": False}
        continue
    data = path.read_bytes()
    code = data.decode("utf-8")
    files[name] = {"present": True, "sha256": hashlib.sha256(data).hexdigest(),
                   "currentCoolingName": "SourceCoolingError" in code,
                   "oldCoolingName": "SourceCooldownError" in code,
                   "quietProgress": "quiet_progress()" in code,
                   "disableTqdm": 'kwargs["disable"] = True' in code,
                   "sharedCooldownClassifier": "market_cooldown" in code}
    if name == "runtime/source_execution.py":
        for setting in ("GLOBAL_LIMIT", "SOURCE_LIMIT", "LEASE_SECONDS", "RENEW_SECONDS"):
            match = re.search(r"^"+setting+r"\s*=\s*(\d+)\s*$", code, re.M)
            files[name][setting] = int(match.group(1)) if match else None
record = {"python": sys.executable, "diagnosticCwd": os.getcwd(),
          "pid1Executable": link("/proc/1/exe"), "pid1Cwd": link("/proc/1/cwd"),
          "appDirectory": str(root) if root else None, "akshareVersion": ak_version,
          "APP_ENV": os.environ.get("APP_ENV") if os.environ.get("APP_ENV") in {"dev", "prod"} else "UNSET_OR_OTHER",
          "files": files}
print(json.dumps(record, ensure_ascii=False, indent=2))
PY
```

`python`是本次诊断解释器，`/proc/1/exe`和`/proc/1/cwd`是容器PID1的实际解释器/启动目录；若PID1是supervisor而非业务进程，应在已确认的业务PID核对同样两项，不打印完整命令行。当前Dockerfile/Compose使用`exec python -m app serve`。核对AKShare应为1.18.97，部署目录应与`appDirectory`一致。文件哈希可与准备部署的同版本文件对照；容器磁盘代码与长期运行进程的已加载代码也可能不同，不能只看宿主机仓库更新。

### 2. 仅查看保护键TTL

在**确认解释器与配置来源后**执行。此脚本仅TTL查询，不读取键值、不清除/续期键、不输出连接URL或凭据，不扫描完整Redis、不调用业务刷新。默认从容器已注入的`.env.prod`环境获取连接配置；本地直接执行默认`.env.dev`。

```bash
docker compose exec -T be-data-analysis python - <<'PY'
import json
import sys
import redis
from redis.backoff import NoBackoff
from redis.retry import Retry
from app.core.config import load_settings

keys = [
    "stock:market:v1:cooldown:ths", "stock:market:v1:cooldown:sina-index",
    *["stock:market:v1:cooldown:module:"+name for name in
      ("industrySectors", "conceptSectors", "marketFundFlow", "coreIndices")],
    "stock:etf-monitor:v1:sina:cooldown", "stock:monitor:v1:xq:cooldown",
    "stock:market:v1:min-interval", "stock:market:v1:lock",
    "stock:etf-monitor:v1:min-interval", "stock:etf-monitor:v1:lock",
    "stock:monitor:v1:sample:lock",
]
client = None
try:
    settings = load_settings()
    client = redis.Redis.from_url(settings.redis_url.get_secret_value(),
        decode_responses=True, socket_connect_timeout=1, socket_timeout=1,
        retry=Retry(NoBackoff(), 0))
    print(json.dumps({key: client.ttl(key) for key in keys}, ensure_ascii=False, indent=2))
except Exception as exc:
    print(json.dumps({"status": "READ_FAILED", "exceptionType": type(exc).__name__}))
    sys.exit(1)
finally:
    if client is not None:
        client.close()
PY
```

TTL正数表示剩余秒数，`-2`表示键不存在，`-1`表示键无过期时间；TTL本身**不能证明首次冷却原因**。可以相隔一段时间重复只读TTL，观察是否自然下降，但不据此清键或批量重试。旧版本写入的两小时THS共享冷却**不会因代码升级自动消失**；本轮使用NX且不主动清理已有保护，须尊重现有TTL，不能仅因新规则为模块300秒就绕过可能的403/429保护。

若这些只读信息仍不能定位，补充当前实例首次失败前后的脱敏日志（接口、模块、耗时、异常/底层类型、HTTP、分类，以及`reason/fields/badRows/code`），不要回传Token、Cookie、URL凭据或完整缓存。本轮仅离线核对，未执行上述生产命令，未清除冷却、未真实重试。
