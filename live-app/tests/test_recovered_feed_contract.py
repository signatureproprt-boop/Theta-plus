import struct
import pytest
from sdk_feeds import split_live_packets

@pytest.mark.parametrize('declared',[70,162])
def test_full_frames_keep_exact_wire_bytes_and_following_ticker(declared):
    full=struct.pack('<BHBI',8,declared,2,123)+bytes(154)
    ticker=struct.pack('<BHBIfI',2,16,0,13,22888.0,1792554000)
    assert list(split_live_packets(full+ticker))==[full,ticker]

@pytest.mark.parametrize('frame',[b'\x08',struct.pack('<BHBI',8,70,2,123),struct.pack('<BHBI',2,0,0,13)])
def test_truncated_or_unknown_lengths_fail_without_claiming_data(frame):
    with pytest.raises(ValueError):list(split_live_packets(frame))

def test_undecoded_subscription_header_is_not_counted_as_observed(tmp_path):
    from collection import Collector
    from store import Store
    from types import SimpleNamespace
    collector=Collector(SimpleNamespace(),Store(tmp_path/'test.db'))
    task={'family':'live','item':{'security_id':'13','segment':'IDX_I'},'batch':[{'security_id':'13','segment':'IDX_I'}]}
    response={'raw':{'frames':[{'observations':[{'security_id':'13','packet_code':1}]}]}}
    result=collector.persist_result(task,response)
    assert result['rows']==0 and result['missing_security_ids']==['13']
