# 直接脚本入口不能让app/calendar遮蔽标准库calendar；模块入口沿用原搜索路径。
if __name__ == "__main__" and not __package__:
    import sys
    from pathlib import Path
    sys.path[0] = str(Path(__file__).resolve().parent.parent)

# 新浪指数
# import akshare as ak
#
# stock_zh_index_spot_sina_df = ak.stock_zh_index_spot_sina()
# print(stock_zh_index_spot_sina_df)

# 基金实时行情-新浪
# import akshare as ak
#
# fund_etf_category_sina_df = ak.fund_etf_category_sina(symbol="ETF基金")
# print(fund_etf_category_sina_df)

# 基金持仓资产比例
# import akshare as ak
#
# fund_individual_detail_hold_xq_df = ak.fund_individual_detail_hold_xq(symbol="588000", date="20261009")
# print(fund_individual_detail_hold_xq_df)

# 基金持仓
# import akshare as ak
#
# fund_portfolio_hold_em_df = ak.fund_portfolio_hold_em(symbol="588000", date="2026")
# print(fund_portfolio_hold_em_df)

# 同花顺-个股资金流
# import akshare as ak
#
# stock_fund_flow_individual_df = ak.stock_fund_flow_individual(symbol="即时")
# print(stock_fund_flow_individual_df)

# 同花顺个股资金服务端筛选演示：导入无网络，仅显式运行下面命令才请求一页基线。
# python -m app.demo --code 601991 --no-proxy
# 无已证实的代码筛选参数，不默认猜测code/stockcode；实验参数须由调用者显式提供：
# python -m app.demo --code 601991 --experimental-query '{"由调用者指定的参数名":"601991"}'
# 仅观测原始返回的全部股票代码，不下载后续分页，不用本地过滤伪装服务端筛选。
