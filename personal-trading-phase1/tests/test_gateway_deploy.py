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


def test_missing_fence_is_distinct_from_permission_failure(monkeypatch):
    from types import SimpleNamespace
    spec=importlib.util.spec_from_file_location('owner_merge',Path(__file__).parents[1]/'deploy/merge_cloudshell.py')
    owner=importlib.util.module_from_spec(spec);spec.loader.exec_module(owner)
    monkeypatch.setattr(owner.subprocess,'run',lambda *a,**kw:SimpleNamespace(returncode=1,
        stderr='One or more URLs matched no objects.',stdout=''))
    assert owner.current_fence()==({},'0')
    monkeypatch.setattr(owner.subprocess,'run',lambda *a,**kw:SimpleNamespace(returncode=1,
        stderr='403 permission denied (or it does not exist)',stdout=''))
    with pytest.raises(RuntimeError,match='CANNOT_READ'):owner.current_fence()


def gateway_service():
    import json
    return {'spec':{'template':{'metadata':{'annotations':{
        'run.googleapis.com/network-interfaces':json.dumps([{'network':'default','subnetwork':module.SUBNET}]),
        'run.googleapis.com/vpc-access-egress':'private-ranges-only'}},
        'spec':{'containers':[{'env':[{'name':'THETA_UNIFIED_UPSTREAM','value':'http://10.160.0.2:8000'}]}]}}}}


def test_retry_accepts_only_exact_existing_private_gateway():
    import json
    service=gateway_service()
    assert module.existing_gateway_network(service,'project','region','10.160.0.2')
    service['spec']['template']['metadata']['annotations']['run.googleapis.com/network-interfaces']=json.dumps([{
        'network':'projects/project/global/networks/default',
        'subnetwork':'projects/project/regions/region/subnetworks/'+module.SUBNET}])
    assert module.existing_gateway_network(service,'project','region','10.160.0.2')
    with pytest.raises(RuntimeError):module.existing_gateway_network(service,'other-project','region','10.160.0.2')
    with pytest.raises(RuntimeError):module.existing_gateway_network(service,'project','region','10.160.0.3')


@pytest.mark.parametrize('change',[
    {'run.googleapis.com/network-interfaces':'not-json'},
    {'run.googleapis.com/network-interfaces':'[]'},
    {'run.googleapis.com/network-interfaces':'[{"network":"default","subnetwork":"other"}]'},
    {'run.googleapis.com/vpc-access-egress':'all-traffic'},
    {'run.googleapis.com/vpc-access-connector':'existing-connector'},
    {'run.googleapis.com/network-interfaces':'[{"network":"default","subnetwork":"theta-unified-gateway","tags":"unrelated"}]'},
])
def test_retry_rejects_unrelated_network_settings(change):
    service=gateway_service()
    service['spec']['template']['metadata']['annotations'].update(change)
    with pytest.raises(RuntimeError,match='REQUIRES_REVIEW'):
        module.existing_gateway_network(service,'project','region','10.160.0.2')


def test_retry_rollback_preserves_preexisting_private_network():
    cloud=Mock()
    gateway=module.GatewayHandover(cloud,Mock(),Mock(),'project','region','zone','vm')
    gateway.reused_network=True
    gateway.template_changed=True
    gateway.candidate='candidate'
    gateway.service=lambda:{'status':{'traffic':[{'revisionName':'candidate','tag':'unified-check'}]}}
    gateway.rollback()
    assert cloud.call_count==1
    assert '--remove-tags=unified-check' in cloud.call_args.args
    assert '--clear-network' not in cloud.call_args.args
