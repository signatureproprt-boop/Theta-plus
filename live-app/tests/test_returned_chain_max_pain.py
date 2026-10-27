from research import returned_strike_max_pain


def rows():
    return [dict(expiry='2026-11-05', strike=k, side=s, oi=o) for k,s,o in [(90,'CE',100),(90,'PE',50),(100,'CE',50),(100,'PE',100),(110,'CE',10),(110,'PE',20)]]


def test_returned_strike_max_pain_and_scope():
    result = returned_strike_max_pain(rows(), '2026-11-05')
    assert result['strikes'] == [100]
    assert result['strike_count'] == 3
    assert 'not verified' in result['scope']


def test_incomplete_duplicate_wrong_expiry_and_empty_oi_are_unavailable():
    data = rows()
    assert returned_strike_max_pain(data[:-1], '2026-11-05') is None
    assert returned_strike_max_pain(data + [data[0]], '2026-11-05') is None
    assert returned_strike_max_pain(data, '2026-11-12') is None
    assert returned_strike_max_pain([dict(x, oi=0) for x in data], '2026-11-05') is None


def test_equal_minima_are_all_returned():
    data = [dict(expiry='2026-11-05', strike=k, side=s, oi=o) for k,s,o in [(100,'CE',0),(100,'PE',1),(110,'CE',1),(110,'PE',0)]]
    assert returned_strike_max_pain(data, '2026-11-05')['strikes'] == [100, 110]
