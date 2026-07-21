# 百度财经数据源说明

## 接口选择

[吾爱破解参考帖子](https://www.52pojie.cn/thread-2114389-1-3.html)分析的是百度财经部分受保护请求，重点参数包括 Cookie `ab_sr` 和请求头 `acs-token`。帖子给出的浏览器指纹、AES-CBC 和哈希流程并不完整，关键常量也被遮蔽，不适合作为稳定生产依赖。

本服务使用无需鉴权的公开日 K 接口：

```text
GET https://finance.pae.baidu.com/selfselect/getstockquotation
```

核心参数为 `code`、`group=quotation_kline_ab`、`ktype=1`、`newFormat=1` 和 `finClientType=pc`。股票与场内 ETF 均按六位代码请求。

## 数据处理

百度返回 `newMarketData.keys` 和逗号分隔的 `marketData`。处理层先按动态 `keys` 组装记录，再转换为领域字段，避免接口调整字段顺序后产生静默错位。

必需字段为交易日期、开高低收、成交量和成交额。涨跌额、涨跌幅、换手率、前收盘及 MA5/10/20 允许为空。单条损坏记录会被跳过；非空响应全部无法解析时视为数据源故障。结果按交易日期去重、升序排列，并在服务层再次按请求区间过滤。

## 名称解析

百度日 K 接口仅接受代码。股票名称由独立的东方财富公开建议接口精确解析，仅保留沪 A、深 A、北 A 和六位代码。解析成功结果在内存中短期缓存；不进行模糊匹配，也不持久化证券目录。

## Cookie 安全

`BAIDU_AB_SR` 默认为空。配置后使用秘密类型保存，只能由百度客户端读取，并且仅当目标主机严格等于 `finance.pae.baidu.com` 时加入 Cookie Jar。异常和日志不得包含 Cookie、完整请求头或响应中的内部详情。

该 Cookie 不会发送给名称解析器，不能代替 `acs-token`，也不会启用盘中受保护接口。
