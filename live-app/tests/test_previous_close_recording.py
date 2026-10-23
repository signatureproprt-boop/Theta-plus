import struct
from datetime import datetime,timezone,timedelta
from dhanhq.marketfeed import MarketFeed
from data_recorder import Journal
from store import Store


def test_previous_close_before_tick_preserves_source_and_never_generates_a_tick(tmp_path):
    clock=datetime(2026,10,23,10,tzinfo=timezone.utc)
    s=Store(tmp_path/'s.db');j=Journal(s,'live',[{'segment':'IDX_I','security_id':'13','symbol':'NIFTY'}],'revision',clock=lambda:clock)
    codec=object.__new__(MarketFeed)
    previous=struct.pack('<BHBIfI',6,16,0,13,22687.65,0)
    assert j.append(previous,codec)==[] and j.latest=={}
    tick=struct.pack('<BHBIfI',2,16,0,13,22600,int(clock.timestamp())+19800)
    values=j.append(tick,codec);assert len(values)==1 and values[0]['ltp']==22600
    j.flush();row=s.document('system/latest/live')[0]['rows']['0:13']
    assert row['previous_close']==22687.65 and row['trade_at']==clock.isoformat()
    assert row['previous_close_received_at']==clock.isoformat() and row['quote_at'] is None


def test_later_previous_close_updates_baseline_without_freshening_an_old_trade(tmp_path):
    clock=[datetime(2026,10,23,10,tzinfo=timezone.utc)]
    j=Journal(Store(tmp_path/'s.db'),'live',[{'segment':'IDX_I','security_id':'13'}],'rev',clock=lambda:clock[0]);codec=object.__new__(MarketFeed)
    tick=struct.pack('<BHBIfI',2,16,0,13,100,int(clock[0].timestamp())+19800);j.append(tick,codec)
    received=j.latest['0:13']['received_at'];clock[0]+=timedelta(minutes=10)
    assert j.append(struct.pack('<BHBIfI',6,16,0,13,99,0),codec)==[]
    assert j.latest['0:13']['received_at']==received and j.latest['0:13']['previous_close']==99
    j.append(struct.pack('<BHBIfI',6,16,0,13,0,0),codec)
    assert j.latest['0:13']['previous_close']==99
