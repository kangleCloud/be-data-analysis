"""仅审计即时个股资金的稳定代码分页，继续由原AKShare函数解析金额。"""

import logging
import re
from urllib.parse import urlsplit, urlunsplit

from app.market.normalize import SourceDataError

LOGGER = logging.getLogger(__name__)
INITIAL = '/funds/ggzjl/field/code/order/desc/ajax/1/free/1/'
PAGE = re.compile(r'^/funds/ggzjl/field/(zdf|code)/order/desc/page/(\d+)/ajax/1/free/1/$')


class IndividualPaging:
    def __init__(self, call):
        self.enabled = call.function == 'stock_fund_flow_individual' and call.parameters.get('symbol') == '即时'
        self.total, self.next_page, self.last_code = None, 1, None
        self.codes, self.row_count = set(), 0

    def prepare(self, request):
        url = urlsplit(request.url)
        if not self.enabled or url.hostname != 'data.10jqka.com.cn':
            return None
        if url.path == INITIAL:
            return 0
        match = PAGE.fullmatch(url.path)
        if not match:
            return None
        request.url = urlunsplit(url._replace(path=url.path.replace('/field/zdf/','/field/code/')))
        return int(match[2])

    def fail(self, reason, code=None, rows=1):
        raise SourceDataError('同花顺个股资金分页不完整或不稳定',reason=reason,
                              fields=('股票代码','页码'),code=code,bad_rows=rows)

    def response(self, page, response):
        if page is None:
            return
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(response.text,'lxml')
        info = soup.find('span',class_='page_info')
        match = re.fullmatch(r'\s*(\d+)\s*/\s*(\d+)\s*',info.get_text() if info else '')
        if (page == 0 or info is not None) and (not match or int(match[2]) < 1):
            self.fail('MISSING_PAGE_INFO')
        count = int(match[2]) if match else self.total
        if page == 0:
            if self.total is not None:
                self.fail('REPEATED_PAGE_COUNT')
            self.total = count
            return
        if self.total is None or count != self.total or page != self.next_page or (match and int(match[1]) != page):
            self.fail('MISSING_OR_OUT_OF_ORDER_PAGE')
        table = soup.find('table')
        codes = []
        if table:
            for row in table.find_all('tr'):
                cells = row.find_all('td')
                if not cells:
                    continue
                code = cells[1].get_text(strip=True) if len(cells) > 1 else ''
                if not re.fullmatch(r'\d{1,6}',code):
                    self.fail('INVALID_PAGE_CODE')
                codes.append(code.zfill(6))
        if not codes:
            self.fail('EMPTY_PAGE')
        for code in codes:
            if code in self.codes:
                self.fail('DUPLICATE_PAGE_CODE',code)
            if self.last_code is not None and code >= self.last_code:
                self.fail('UNSTABLE_CODE_ORDER',code)
            self.codes.add(code)
            self.last_code = code
        self.row_count += len(codes)
        self.next_page += 1
        LOGGER.info('即时个股资金分页 page=%d/%d rows=%d uniqueCodes=%d',page,self.total,len(codes),len(self.codes))

    def finish(self, rows):
        if self.enabled and (self.total is None or self.next_page != self.total+1 or len(rows) != self.row_count):
            self.fail('INCOMPLETE_PAGES',rows=abs((self.total or 0)-(self.next_page-1)))
