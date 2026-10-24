import importlib.util
from pathlib import Path
from unittest.mock import Mock
import pytest

SPEC=importlib.util.spec_from_file_location('gateway_deploy',Path(__file__).parents[1]/'deploy/gateway_cloudshell.py')
module=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(module)


def test_gateway_firewall_must_not_broaden_source_or_target():
    rule={'network':'projects/example/global/networks/default','direction':'INGRESS','priority':900,
        'sourceRanges':[module.CIDR],'targetTags':[module.TARGET_TAG],
        'allowed':[{'IPProtocol':'tcp','ports':['8000']}]}
    module.validate_rule(rule,'ALLOW',module.CIDR,900)
    for change in ({'sourceRanges':['0.0.0.0/0']},{'priority':1000},{'targetTags':['another-app']},{'disabled':True}):
        with pytest.raises(RuntimeError):module.validate_rule({**rule,**change},'ALLOW',module.CIDR,900)


def test_gateway_traffic_change_stops_promotion():
    cloud=Mock()
    gateway=module.GatewayHandover(cloud,Mock(),Mock(),'project','region','zone','vm')
    gateway.baseline={'status':{'traffic':[{'revisionName':'original','percent':100}]}}
    gateway.service=lambda:{'status':{'traffic':[{'revisionName':'changed-by-owner','percent':100}]}}
    with pytest.raises(RuntimeError,match='TRAFFIC_CHANGED'):gateway.promote()
    cloud.assert_not_called()


def test_private_api_bind_has_no_public_default():
    import yaml
    root=Path(__file__).parents[1]
    vps=yaml.safe_load((root/'compose.vps.yaml').read_text())
    assert vps['services']['api']['ports']==['${THETA_PRIVATE_IP:-127.0.0.1}:8000:8000']
