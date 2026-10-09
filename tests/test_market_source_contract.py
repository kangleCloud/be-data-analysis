"""固定 AKShare 1.18.97 的表格解析离线复现，不请求真实同花顺。"""

from types import SimpleNamespace

import akshare
import pandas as pd
import pytest
import requests

from app.normalize import SourceDataError, normalize_individual_batch, normalize_sectors
from app.providers.http import quiet_progress


def table(values):
    frame = pd.DataFrame(values, columns=['序号', *[f'列{i}' for i in range(len(values[0])-1)]])
    return '<span class="page_info">1/1</span>'+frame.to_html(index=False)


@pytest.mark.parametrize('function,sector', [('stock_fund_flow_industry','industry'),('stock_fund_flow_concept','concept')])
def test_fixed_akshare_sector_parser_columns_units_and_optional_values(monkeypatch, function, sector):
    module = __import__(getattr(akshare,function).__module__, fromlist=['dummy'])
    monkeypatch.setattr(module.py_mini_racer,'MiniRacer', lambda: SimpleNamespace(eval=lambda text: None, call=lambda name:'offline'))
    monkeypatch.setattr(module,'_get_file_content_ths',lambda name:'')
    html = table([[1,'半导体',1234.5,'3.5%',2,.5,1.5,55,None,'—',None]])
    calls = []
    def send(session, request, **kwargs):
        calls.append(request.url)
        response = requests.Response()
        response.status_code, response._content = 200, html.encode()
        response.encoding = 'utf-8'
        return response
    monkeypatch.setattr(requests.Session,'send',send)
    with quiet_progress():
        frame = getattr(akshare,function)(symbol='即时')
    item = normalize_sectors(frame,sector)['items'][0]
    assert (item['inflow'],item['outflow'],item['netAmount']) == (2e8,.5e8,1.5e8)
    assert item['changePct'] == 3.5 and item['leader'] is None and item['leaderChangePct'] is None
    assert len(calls) == 2  # 页数与第一页，函数内部保持串行。


def test_fixed_akshare_individual_optional_net_and_invalid_required_money(monkeypatch):
    module = __import__(akshare.stock_fund_flow_individual.__module__, fromlist=['dummy'])
    monkeypatch.setattr(module.py_mini_racer,'MiniRacer',lambda: SimpleNamespace(eval=lambda text:None,call=lambda name:'offline'))
    monkeypatch.setattr(module,'_get_file_content_ths',lambda name:'')
    html = [table([[1,1,'平安银行',10,'-0.2%','1%', '1亿','0.2亿', '—','3亿']])]
    def send(session,request,**kwargs):
        response = requests.Response()
        response.status_code, response._content = 200, html[0].encode()
        response.encoding = 'utf-8'
        return response
    monkeypatch.setattr(requests.Session,'send',send)
    with quiet_progress():
        frame = akshare.stock_fund_flow_individual(symbol='即时')
    market, points = normalize_individual_batch(frame,'now')
    assert market['latest']['netAmount'] == 8e7
    assert list(points) == ['000001']
    html[0] = table([[1,1,'平安银行',10,'-0.2%','1%', '—','0.2亿', '—','3亿']])
    with quiet_progress():
        frame = akshare.stock_fund_flow_individual(symbol='即时')
    with pytest.raises(SourceDataError) as error:
        normalize_individual_batch(frame,'now')
    assert error.value.reason == 'INVALID_VALUES'
    assert error.value.fields == ('流入资金',) and error.value.code == '000001' and error.value.bad_rows == 1


def test_market_aggregate_overflow_is_diagnosed_before_publish():
    rows = [{'股票代码':code,'股票简称':'测试','涨跌幅':1,'流入资金':1e308,'流出资金':0} for code in ('600000','600001')]
    with pytest.raises(SourceDataError) as error:
        normalize_individual_batch(rows,'now')
    assert error.value.reason == 'AGGREGATE_OVERFLOW'
    assert error.value.fields == ('流入资金', '净额')
    assert error.value.bad_rows == 2
