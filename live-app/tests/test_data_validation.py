import asyncio
from datetime import date,datetime
import json
from zoneinfo import ZoneInfo
import httpx
import pytest
from dhan_data import fetch_history,option_count,verify_record
from history_coverage import coverage_report
from provider import Dhan,ProviderError
from store import Store


def stamp(day='2026-10-12',at='09:15'):
    return datetime.fromisoformat(day+'T'+at+':00').replace(tzinfo=ZoneInfo('Asia/Kolkata')).timestamp()


def candles(stamps=None):
    stamps=[stamp()] if stamps is None else stamps
    return dict(timestamp=stamps,open=[10]*len(stamps),high=[12]*len(stamps),low=[9]*len(stamps),close=[11]*len(stamps),volume=[100]*len(stamps))


def option():
    return {'data':{'ce':{**candles(),**{k:[1] for k in ('iv','oi','strike','spot')}}}}


class Provider:
    def __init__(self):self.calls=[]
    async def post(self,path,body):self.calls.append((path,body));return candles()


def test_history_fetch_includes_opening_minute_and_records_exact_request(tmp_path):
    p=Provider();s=Store(tmp_path/'s.db')
    result=asyncio.run(fetch_history(p,s,kind='index',index='nifty',start=date(2026,10,12),end=date(2026,10,13),instrument={'security_id':'13','segment':'IDX_I','instrument':'INDEX'}))
    assert p.calls[0][1]['fromDate']=='2026-10-12 00:00:00'
    saved=s.get('dhan_history',result['id'])['data']
    assert saved['request']==p.calls[0][1] and saved['metadata']['request_version']==2
    assert saved['quality']['regular_session_rows']==1


def test_daily_matches_documented_payload_and_has_daily_quality(tmp_path):
    p=Provider();s=Store(tmp_path/'s.db')
    result=asyncio.run(fetch_history(p,s,kind='daily',index='nifty',start=date(2026,10,12),end=date(2026,10,13),instrument={'security_id':'13','segment':'IDX_I','instrument':'INDEX'}))
    assert p.calls[0][0]=='charts/historical'
    body=p.calls[0][1]
    assert body['expiryCode']==0 and 'interval' not in body and body['fromDate']=='2026-10-12'
    assert result['quality']['rows']==1 and 'sessions' not in result['quality']


def test_option_required_arrays_and_finite_values_are_enforced():
    raw=option();del raw['data']['ce']['iv']
    with pytest.raises(ProviderError):option_count(raw,'CALL')
    raw=option();raw['data']['ce']['close']=[float('nan')]
    with pytest.raises(ProviderError):option_count(raw,'CALL')


def test_quality_counts_boundaries_and_session_rows_without_altering_raw():
    raw=candles([stamp(),stamp(at='15:30'),stamp('2026-10-13')])
    q=verify_record({'metadata':{'kind':'index','from':'2026-10-12','to_exclusive':'2026-10-13'},'raw':raw})
    assert q['rows']==3 and q['regular_session_rows']==1
    assert q['outside_requested_dates']==1 and q['off_session_rows']==1
    assert len(raw['timestamp'])==3


def test_coverage_deduplicates_overlap_and_excludes_late_rows(tmp_path):
    s=Store(tmp_path/'s.db');meta={'kind':'index','index':'nifty','security_id':'13'}
    first=s.add('dhan_history',{'metadata':meta,'raw':candles([stamp(),stamp(at='15:30')])})
    s.add('dhan_history',{'metadata':meta,'raw':candles()})
    q=coverage_report(s,date(2026,10,12),date(2026,10,13),True)['groups'][0]
    assert q['rows']==1 and q['overlap_rows']==1 and q['off_session_rows_excluded']==1
    assert q['sessions'][0]['missing']==374 and not q['weekday_grid_complete']
    assert len(s.get('dhan_history',first)['data']['raw']['timestamp'])==2
    bad=candles();bad['close']=[10]
    s.add('dhan_history',{'metadata':meta,'raw':bad})
    q=coverage_report(s,date(2026,10,12),date(2026,10,13),True)['groups'][0]
    assert q['rows']==0 and q['conflicting_rows']==1


def test_coverage_never_mixes_option_variants(tmp_path):
    s=Store(tmp_path/'s.db')
    for code in (1,2):
        meta={'kind':'expired_option','index':'nifty','side':'CALL','strike':'ATM','expiry_flag':'MONTH','expiry_code':code,'security_id':'13'}
        s.add('dhan_history',{'metadata':meta,'raw':option()})
    q=coverage_report(s,date(2026,10,12),date(2026,10,13))
    assert len(q['groups'])==2
    assert all(g['fixed_contract_backtest_ready'] is False for g in q['groups'])


class Credentials:
    configured=True
    client_id='fixture-client'
    def token(self):return 'fixture-token'


def test_provider_timeout_is_typed_and_bounded(monkeypatch):
    original=httpx.AsyncClient
    def handler(request):
        assert request.extensions['timeout']['read']==60
        raise httpx.ReadTimeout('sensitive URL omitted',request=request)
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
    with pytest.raises(ProviderError) as exc:asyncio.run(Dhan(Credentials()).post('charts/historical',{}))
    assert exc.value.code=='TIMEOUT' and exc.value.status==504
    assert 'sensitive' not in str(exc.value)


def test_provider_reason_redacts_credentials(monkeypatch):
    original=httpx.AsyncClient
    response={'errorCode':'DH-905','errorMessage':'Bad fixture-token fixture-client'}
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(lambda r:httpx.Response(400,json=response)),**kw))
    with pytest.raises(ProviderError) as exc:asyncio.run(Dhan(Credentials()).post('charts/historical',{}))
    assert 'Bad' in str(exc.value) and 'fixture' not in str(exc.value)
