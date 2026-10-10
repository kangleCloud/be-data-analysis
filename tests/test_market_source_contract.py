"""固定 AKShare 1.18.97 的表格解析离线复现，不请求真实同花顺。"""

from types import SimpleNamespace

import akshare
import pandas as pd
import pytest
import requests

from app.market.normalize import SourceDataError, normalize_individual_batch, normalize_sectors
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


def paging_html(codes,page=None,total=2):
    rows = [[i,code,'测试',10,'1%','1%','1亿','0.2亿','0.8亿','3亿'] for i,code in enumerate(codes,1)]
    frame = pd.DataFrame(rows,columns=['序号',*[f'列{i}' for i in range(9)]])
    return (f'<span class="page_info">{page}/{total}</span>' if page else '')+frame.to_html(index=False)


def test_fixed_akshare_multi_page_rewrites_only_instant_individual_and_reuses_batch(monkeypatch):
    from app.runtime.source_execution import SourceCall,SourceControl,controlled_http
    from tests.test_source_execution import ControlRedis
    module = __import__(akshare.stock_fund_flow_individual.__module__,fromlist=['dummy'])
    monkeypatch.setattr(module.py_mini_racer,'MiniRacer',lambda:SimpleNamespace(eval=lambda text:None,call=lambda name:'offline'))
    monkeypatch.setattr(module,'_get_file_content_ths',lambda name:'')
    backend,clock = ControlRedis(),[0.0]
    backend.rate_now = lambda:clock[0]*1000
    monkeypatch.setattr('app.runtime.source_execution.time.monotonic',lambda:clock[0])
    monkeypatch.setattr('app.runtime.source_execution.time.sleep',lambda t:clock.__setitem__(0,clock[0]+t))
    urls=[]
    def send(session,request,**kwargs):
        urls.append(request.url)
        if '/page/1/' in request.url:
            html = paging_html(['600001','600000'])
        elif '/page/2/' in request.url:
            html = paging_html(['300001','000001'],2)
        else:
            html = '<span class="page_info">1/2</span>'
        response = requests.Response()
        response.status_code,response._content,response.encoding = 200,html.encode(),'utf-8'
        return response
    monkeypatch.setattr(requests.Session,'send',send)
    control = SourceControl(backend)
    keys = control.acquire('ths','token')
    call = SourceCall('stock_fund_flow_individual','ths',{'symbol':'即时'},domains=('data.10jqka.com.cn',))
    with controlled_http(control,call,keys,'token',None,20,15) as audit,quiet_progress():
        frame = akshare.stock_fund_flow_individual(symbol='即时')
        audit.finish(frame.to_dict('records'))
    assert len(urls)==3 and all('/field/code/' in url for url in urls)
    assert audit.row_count==4 and audit.next_page==3 and clock[0]==pytest.approx(2)
    market,points = normalize_individual_batch(frame,'now')
    assert market['latest']['inflow']==4e8 and market['latest']['outflow']==8e7
    assert market['latest']['netAmount']==3.2e8 and len(points)==4
    assert points['600000']['netAmount']==8e7


@pytest.mark.parametrize('function,symbol,domain,path',[
    ('stock_fund_flow_industry','即时','data.10jqka.com.cn','/funds/hyzjl/field/tradezdf/order/desc/page/1/ajax/1/free/1/'),
    ('stock_fund_flow_concept','即时','data.10jqka.com.cn','/funds/gnzjl/field/tradezdf/order/desc/page/1/ajax/1/free/1/'),
    *[('stock_fund_flow_individual',symbol,'data.10jqka.com.cn','/funds/ggzjl/field/zdf/order/desc/page/1/ajax/1/free/1/') for symbol in ('3日排行','5日排行','10日排行','20日排行')],
    ('stock_fund_flow_individual','即时','elsewhere.test','/funds/ggzjl/field/zdf/order/desc/page/1/ajax/1/free/1/'),
])
def test_paging_does_not_rewrite_other_calls(function,symbol,domain,path):
    from app.runtime.source_execution import SourceCall
    from app.providers.ths_paging import IndividualPaging
    request = requests.Request('GET','http://'+domain+path).prepare()
    audit = IndividualPaging(SourceCall(function,'ths',{'symbol':symbol}))
    original = request.url
    assert audit.prepare(request) is None
    assert request.url == original


@pytest.mark.parametrize('pages,reason',[
    ([(['600001'],1),(['600001'],2)],'DUPLICATE_PAGE_CODE'),
    ([(['600000'],1),(['600001'],2)],'UNSTABLE_CODE_ORDER'),
    ([(['600000'],2)],'MISSING_OR_OUT_OF_ORDER_PAGE'),
    ([([],1)],'EMPTY_PAGE'),
])
def test_paging_rejects_duplicate_unstable_out_of_order_and_empty(pages,reason):
    from app.runtime.source_execution import SourceCall
    from app.providers.ths_paging import IndividualPaging
    audit = IndividualPaging(SourceCall('stock_fund_flow_individual','ths',{'symbol':'即时'}))
    audit.response(0,SimpleNamespace(text='<span class="page_info">1/2</span>'))
    with pytest.raises(SourceDataError) as exc:
        for codes,page in pages:
            audit.response(page,SimpleNamespace(text=paging_html(codes,page) if codes else '<span class="page_info">1/2</span>'))
    assert exc.value.reason == reason


def test_paging_requires_initial_counter_and_all_pages_raw_count():
    from app.runtime.source_execution import SourceCall
    from app.providers.ths_paging import IndividualPaging
    audit = IndividualPaging(SourceCall('stock_fund_flow_individual','ths',{'symbol':'即时'}))
    with pytest.raises(SourceDataError,match='分页'):
        audit.response(0,SimpleNamespace(text='<html></html>'))
    audit.response(0,SimpleNamespace(text='<span class="page_info">1/2</span>'))
    audit.response(1,SimpleNamespace(text=paging_html(['600000'])))
    with pytest.raises(SourceDataError) as exc:
        audit.finish([{}])
    assert exc.value.reason == 'INCOMPLETE_PAGES'
    audit.response(2,SimpleNamespace(text=paging_html(['000001'])))
    with pytest.raises(SourceDataError):
        audit.finish([{}])
