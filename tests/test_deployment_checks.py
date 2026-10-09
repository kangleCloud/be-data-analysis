"""文档中的生产只读核对脚本离线验证，不连接任何Redis或源站。"""

import json
import re
from pathlib import Path
from types import SimpleNamespace

from pydantic import SecretStr

ROOT = Path(__file__).resolve().parents[1]


def scripts():
    text = (ROOT/'docs/source-health-check.md').read_text()
    values = re.findall(r"docker compose exec -T be-data-analysis python - <<'PY'\n(.*?)\nPY", text, re.S)
    assert len(values) == 2
    return values


def test_deployment_code_check_shows_features_without_configuration_values(monkeypatch,capsys):
    secret = 'private-redis-password-or-token'
    monkeypatch.setenv('REDIS_URL',secret)
    monkeypatch.setenv('REDIS_PASSWORD',secret)
    monkeypatch.setenv('XUEQIU_TOKEN',secret)
    exec(compile(scripts()[0],'<deployment-check>','exec'),{})
    output = capsys.readouterr().out
    assert secret not in output
    record = json.loads(output)
    assert record['akshareVersion'] == '1.18.97'
    source = record['files']['source_execution.py']
    assert (source['GLOBAL_LIMIT'],source['SOURCE_LIMIT']) == (1,1)
    assert (source['LEASE_SECONDS'],source['RENEW_SECONDS']) == (30,10)
    assert source['currentCoolingName'] and not source['oldCoolingName']
    assert record['files']['providers/http.py']['disableTqdm']
    assert record['files']['source_execution.py']['quietProgress']


def test_documented_redis_check_only_reads_ttl_and_never_prints_connection(monkeypatch,capsys):
    secret = 'private-password'
    calls = []
    class Client:
        def ttl(self,key):
            calls.append(('ttl',key))
            return 7200
        def close(self):
            calls.append(('close',None))
    monkeypatch.setattr('app.core.config.load_settings',lambda:SimpleNamespace(redis_url=SecretStr('redis://:'+secret+'@offline/2')))
    monkeypatch.setattr('redis.Redis.from_url',lambda *args,**kwargs:Client())
    exec(compile(scripts()[1],'<ttl-check>','exec'),{})
    output = capsys.readouterr().out
    assert secret not in output and 'redis://' not in output
    record = json.loads(output)
    assert record['stock:market:v1:cooldown:ths'] == 7200
    assert record['stock:market:v1:cooldown:module:marketFundFlow'] == 7200
    assert set(operation for operation,_ in calls) == {'ttl','close'}
    assert all('snapshot' not in key for key in record)
