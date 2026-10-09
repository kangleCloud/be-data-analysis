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
import akshare as ak

fund_portfolio_hold_em_df = ak.fund_portfolio_hold_em(symbol="588000", date="2026")
print(fund_portfolio_hold_em_df)