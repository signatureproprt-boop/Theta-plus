import json
import math
import pytest
from research_black76 import calculate


def test_known_call_and_json_units():
    out = calculate('CE', 100, 100, 1, .05, .2)
    assert out['price'] == pytest.approx(7.5770821464)
    assert out['model'] == 'Black-76'
    assert out['units']['theta'] == 'per calendar day'
    json.dumps(out, allow_nan=False)


@pytest.mark.parametrize('args', [('CE',100,100,0,.05,.2),('XX',100,100,1,.05,.2),('CE',100,100,1,.05,math.nan),('PE',-1,100,1,.05,.2)])
def test_rejects_invalid_inputs(args):
    with pytest.raises(ValueError):
        calculate(*args)
