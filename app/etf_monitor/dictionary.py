"""新浪全表生成当日轻量ETF字典；缓存不保存行情原表。"""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import redis
from app.etf_monitor.normalize import catalog
from app.stock_monitor.events import monitor_event_lock

KEY = 'stock:etf-monitor:v1:dictionary-source'
SHANGHAI = ZoneInfo('Asia/Shanghai')


def save_dictionary(client,rows,at):
    payload = {'schemaVersion':1,'source':'SINA','collectedAt':at.astimezone(SHANGHAI).isoformat(timespec='seconds'),
               'etfs':[ {key:item[key] for key in ('symbol','code','name','market')} for item in catalog(rows)]}
    with monitor_event_lock(client, key=f"{KEY}:write-lock"):
        old_raw = client.get(KEY)
        try:
            previous = json.loads(old_raw) if old_raw else {}
        except (ValueError, TypeError):
            previous = {}
        if isinstance(previous, dict) and previous.get("collectedAt", "") > payload["collectedAt"]:
            return previous
        if not client.set(KEY,json.dumps(payload,ensure_ascii=False,allow_nan=False),ex=86400):
            raise redis.RedisError('ETF字典缓存写入失败')
    return payload


def load_dictionary(client,at):
    try:
        raw = client.get(KEY)
        payload = json.loads(raw) if raw else None
        if not isinstance(payload,dict) or payload.get('schemaVersion') != 1 or payload.get('source') != 'SINA':
            return None
        timestamp = datetime.fromisoformat(payload['collectedAt'])
        if timestamp.tzinfo is None or timestamp.astimezone(SHANGHAI).date() != at.astimezone(SHANGHAI).date() or timestamp > at:
            return None
        rows = payload['etfs']
        if not isinstance(rows,list) or not rows or any(not isinstance(row,dict) for row in rows):
            return None
        from app.etf_monitor.normalize import etf_symbol
        symbols = set()
        for row in rows:
            symbol = etf_symbol(row.get('symbol'))
            if not symbol or row.get('code') != symbol[2:] or row.get('market') != symbol[:2] or not isinstance(row.get('name'),str) or not row['name'].strip() or symbol in symbols:
                return None
            symbols.add(symbol)
        return payload
    except (ValueError,KeyError,TypeError):
        return None


def dictionary_response(payload):
    return {**payload,'etfs':[{**row,'exchange':'SSE' if row['market'] == 'SH' else 'SZSE',
                             'etfType':'ETF','source':'AKShare.fund_etf_category_sina','listingStatus':None,'listingDate':None,'shareCount':None,
                             'shareDate':None,'trackingIndexCode':None,'trackingIndexName':None} for row in payload['etfs']]}
