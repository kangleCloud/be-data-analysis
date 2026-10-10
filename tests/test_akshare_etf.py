"""ETF Provider 的固定调用参数、冷却作用范围和预算。"""

import pandas as pd
import pytest
from app.providers.akshare_etf import AkShareEtfProvider, EtfSourceError
from app.runtime.source_execution import SourceCallError


class Executor:
    def __init__(self):
        self.calls = []
        self.error = None
    def call(self, call):
        self.calls.append(call)
        if self.error:
            raise self.error
        return pd.DataFrame([{"字段":"基金代码","值":"510050"}])


def test_ths_profile_uses_six_digit_code_and_remaining_budget():
    executor = Executor()
    source = AkShareEtfProvider(15, executor=executor)
    assert source.profile("510050", budget_seconds=12) == [{"字段":"基金代码","值":"510050"}]
    call = executor.calls[0]
    assert call.function == 'fund_info_ths' and call.parameters == {'symbol':'510050'}
    assert call.group == 'ths' and call.domains == ('fund.10jqka.com.cn',)
    assert call.fund_profile and call.budget_seconds == 12
    with pytest.raises(TimeoutError):
        source.profile('510050', budget_seconds=2)
    assert len(executor.calls) == 1


def test_dictionary_does_not_inherit_quote_cooldown():
    executor = Executor()
    AkShareEtfProvider(executor=executor).quotes()
    AkShareEtfProvider(executor=executor, market_quotes=True).quotes()
    dictionary, quote = executor.calls
    assert dictionary.function == quote.function == 'fund_etf_category_sina'
    assert dictionary.parameters == quote.parameters == {'symbol':'ETF基金'}
    assert not dictionary.cooldown_keys and dictionary.cooldown_policy == 'none'
    assert quote.cooldown_keys == ('stock:etf-monitor:v1:sina:cooldown',)
    assert quote.cooldown_policy == 'etf'


@pytest.mark.parametrize('status',[403,429])
def test_parent_preserves_child_http_classification(status):
    executor = Executor()
    metadata = {'exception_type':'HTTPError','root_type':'HTTPError',
                'http_status':status,'category':'HTTP_REJECTED'}
    executor.error = SourceCallError(metadata)
    with pytest.raises(EtfSourceError) as error:
        AkShareEtfProvider(executor=executor).quotes()
    assert error.value.http_status == status
    assert error.value.exception_type == error.value.root_type == 'HTTPError'
    assert error.value.category == 'HTTP_REJECTED'


def test_asset_allocation_has_no_token_argument():
    executor = Executor()
    AkShareEtfProvider(executor=executor).asset_allocation('510050','20260930')
    call = executor.calls[0]
    assert call.function == 'fund_individual_detail_hold_xq'
    assert call.parameters == {'symbol':'510050','date':'20260930','timeout':15}
    assert call.group == 'xq' and call.domains == ('danjuanfunds.com',)
