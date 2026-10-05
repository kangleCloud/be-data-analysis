# 新浪指数
# import akshare as ak
#
# stock_zh_index_spot_sina_df = ak.stock_zh_index_spot_sina()
# print(stock_zh_index_spot_sina_df)

import akshare as ak

if __name__ == "__main__":
    fund_info_ths_df = ak.fund_info_ths(symbol="588000")
    print(fund_info_ths_df)
