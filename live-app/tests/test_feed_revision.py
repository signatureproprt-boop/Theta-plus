from datetime import datetime,timezone
from types import SimpleNamespace
from data_system import subscription_plan

def test_depth_rebalance_does_not_change_unaffected_live_feed_revisions(monkeypatch):
    import data_system
    monkeypatch.setattr(data_system,'now',lambda:datetime(2026,10,23,4,tzinfo=timezone.utc))
    spot=[22500]
    options={str(1000+i*2+side):{'security_id':str(1000+i*2+side),'underlying':'nifty','expiry':'2026-10-27','strike':22000+i*50,'side':'CE' if side==0 else 'PE'} for i in range(40) for side in (0,1)}
    board=SimpleNamespace(instruments={'nifty':{'key':'nifty','security_id':'13','segment':'IDX_I','instrument':'INDEX'}},options=options,
                          view=lambda:{'items':[{'key':'nifty','price':spot[0]}]})
    first=subscription_plan(board,'credential-v1');spot[0]=23500;second=subscription_plan(board,'credential-v1')
    assert first['revision']!=second['revision']
    assert first['feed_revisions']['depth20']!=second['feed_revisions']['depth20']
    for family in ('live','live-extra','depth200'):
        assert first['scopes'][family]==second['scopes'][family]
        assert first['feed_revisions'][family]==second['feed_revisions'][family]
    renewed=subscription_plan(board,'credential-v2')
    assert all(second['feed_revisions'][f]!=renewed['feed_revisions'][f] for f in second['scopes'])
