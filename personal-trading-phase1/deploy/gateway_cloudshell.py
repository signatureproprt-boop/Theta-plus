"""Owner-controlled same-URL cutover through private Direct VPC egress."""
import ipaddress
import json
import os
import time
from datetime import datetime, timezone
from urllib.request import urlopen

SUBNET='theta-unified-gateway'
CIDR='172.31.240.0/24'
TARGET_TAG='personal-trading-phase1-iap'


def existing_gateway_network(service, project, region, ip):
    """Resume only our exact private gateway template; reject unrelated networks."""
    template=service['spec']['template']
    annotations=template.get('metadata',{}).get('annotations',{})
    raw=annotations.get('run.googleapis.com/network-interfaces')
    if annotations.get('run.googleapis.com/vpc-access-connector'):
        raise RuntimeError('EXISTING_FRONTEND_NETWORK_REQUIRES_REVIEW; connector retained')
    if not raw:
        return False
    def resource_matches(value, short, path):
        return value in (short,path,'https://www.googleapis.com/compute/v1/'+path)
    try:
        interfaces=json.loads(raw)
        interface=interfaces[0]
        valid=(len(interfaces)==1 and set(interface)<= {'network','subnetwork'} and
            resource_matches(interface.get('network'),'default',f'projects/{project}/global/networks/default') and
            resource_matches(interface.get('subnetwork'),SUBNET,f'projects/{project}/regions/{region}/subnetworks/{SUBNET}') and
            annotations.get('run.googleapis.com/vpc-access-egress')=='private-ranges-only')
    except (ValueError,TypeError,KeyError,IndexError):
        valid=False
    env=template.get('spec',{}).get('containers',[{}])[0].get('env',[])
    upstream=[e for e in env if e.get('name')=='THETA_UNIFIED_UPSTREAM']
    if not valid or upstream!=[{'name':'THETA_UNIFIED_UPSTREAM','value':'http://'+ip+':8000'}]:
        raise RuntimeError('EXISTING_FRONTEND_NETWORK_REQUIRES_REVIEW; no network settings replaced')
    return True


def validate_rule(rule, action, source, priority):
    expected='allowed' if action=='ALLOW' else 'denied'
    if not (rule.get('network','').rsplit('/',1)[-1]=='default' and
            rule.get('direction')=='INGRESS' and rule.get('priority')==priority and
            rule.get('sourceRanges')==[source] and rule.get('targetTags')==[TARGET_TAG] and
            rule.get(expected)==[{'IPProtocol':'tcp','ports':['8000']}] and not rule.get('disabled',False)):
        raise RuntimeError('EXISTING_GATEWAY_FIREWALL_RULE_DIFFERS')


class GatewayHandover:
    def __init__(self, cloud, remote, ssh, project, region, zone, vm):
        self.cloud,self.remote,self.ssh=cloud,remote,ssh
        self.project,self.region,self.zone,self.vm=project,region,zone,vm
        self.baseline=None;self.promoted=False;self.candidate=None;self.template_changed=False
        self.reused_network=False

    def service(self):
        return json.loads(self.cloud('run','services','describe','theta-plus','--region='+self.region,'--format=json',capture=True))

    @staticmethod
    def traffic(service):
        return {t['revisionName']:t['percent'] for t in service['status']['traffic'] if t.get('percent',0)>0}

    def prepare(self):
        print('MERGE_GATEWAY_PRIVATE_NETWORK_PREPARE',flush=True)
        self.baseline=self.service()
        vm=json.loads(self.cloud('compute','instances','describe',self.vm,'--zone='+self.zone,'--format=json',capture=True))
        interface=vm['networkInterfaces'][0]
        if interface['network'].rsplit('/',1)[-1]!='default' or TARGET_TAG not in vm.get('tags',{}).get('items',[]):
            raise RuntimeError('UNEXPECTED_VM_NETWORK_OR_TAG')
        ip=str(ipaddress.IPv4Address(interface['networkIP']))
        if not ipaddress.ip_address(ip).is_private: raise RuntimeError('PRIVATE_BACKEND_IP_REQUIRED')
        self.reused_network=existing_gateway_network(self.baseline,self.project,self.region,ip)
        if self.reused_network:
            print('EXISTING_PRIVATE_GATEWAY_CONFIGURATION_VERIFIED_FOR_RETRY',flush=True)
        subnets=json.loads(self.cloud('compute','networks','subnets','list','--network=default','--format=json',capture=True))
        existing=next((s for s in subnets if s['name']==SUBNET and s['region'].rsplit('/',1)[-1]==self.region),None)
        if existing:
            if existing['ipCidrRange']!=CIDR: raise RuntimeError('EXISTING_GATEWAY_SUBNET_DIFFERS')
        else:
            ranges=[s['ipCidrRange'] for s in subnets]+[r['ipCidrRange'] for s in subnets for r in s.get('secondaryIpRanges',[])]
            if any(ipaddress.ip_network(CIDR).overlaps(ipaddress.ip_network(r)) for r in ranges):
                raise RuntimeError('GATEWAY_SUBNET_OVERLAP')
            self.cloud('compute','networks','subnets','create',SUBNET,'--network=default','--region='+self.region,'--range='+CIDR,capture=True)
        rules=json.loads(self.cloud('compute','firewall-rules','list','--format=json',capture=True))
        for name,action,source,priority in [('theta-unified-backend-private','ALLOW',CIDR,900),
                ('theta-unified-backend-deny','DENY','0.0.0.0/0',950)]:
            rule=next((r for r in rules if r['name']==name),None)
            if rule: validate_rule(rule,action,source,priority)
            else: self.cloud('compute','firewall-rules','create',name,'--network=default','--direction=INGRESS',
                '--priority='+str(priority),'--action='+action,'--rules=tcp:8000','--source-ranges='+source,
                '--target-tags='+TARGET_TAG,capture=True)
        self.remote('sudo','python3','-c',
            "from pathlib import Path;import sys; p=Path('/srv/personal-trading/.env'); "
            "lines=[s for s in p.read_text().splitlines() if not s.startswith('THETA_PRIVATE_IP=')]; "
            "p.write_text('\\n'.join(lines+['THETA_PRIVATE_IP='+sys.argv[1]])+'\\n')",ip)
        self.remote('sudo','python3','-c',
            "import subprocess,json,os;from pathlib import Path;root=Path('/srv/personal-trading'); "
            "sha=json.loads((root/'release.json').read_text())['sha']; "
            "subprocess.run(['docker','compose','-p','personal-trading-phase1','-f','compose.yaml','-f','compose.vps.yaml',"
            "'up','-d','--no-build','--no-deps','--force-recreate','api'],cwd=root/'current',"
            "env={**os.environ,'PHASE1_APP_IMAGE':'personal-trading-phase1:'+sha},check=True)")
        suffix='unified-'+datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
        self.candidate='theta-plus-'+suffix
        self.template_changed=True
        print('MERGE_GATEWAY_CANDIDATE_DEPLOY',flush=True)
        self.cloud('run','services','update','theta-plus','--region='+self.region,'--network=default','--subnet='+SUBNET,
            '--vpc-egress=private-ranges-only','--update-env-vars=THETA_UNIFIED_UPSTREAM=http://'+ip+':8000',
            '--revision-suffix='+suffix,'--tag=unified-check','--no-traffic','--quiet',capture=True)
        current=self.service()
        tagged=next((t.get('url') for t in current['status']['traffic'] if t.get('tag')=='unified-check' and
                     t.get('revisionName')==self.candidate),None)
        if not tagged: raise RuntimeError('GATEWAY_CANDIDATE_URL_MISSING')
        self.tagged=tagged
        # Retry private networking startup, with no production traffic movement.
        for _ in range(30):
            try:
                with urlopen(tagged+'/api/v2/system',timeout=15) as response:
                    if json.load(response).get('storage')=='PostgreSQL + TimescaleDB': break
            except Exception: pass
            time.sleep(2)
        else: raise RuntimeError('PRIVATE_GATEWAY_CANDIDATE_NOT_READY')
        with urlopen(tagged+'/',timeout=30) as response:
            if b'desk-market-watch' not in response.read(): raise RuntimeError('MERGED_WEBSITE_NOT_VERIFIED')
        print('EXISTING_HTTPS_URL_PRIVATE_GATEWAY_CANDIDATE_VERIFIED',flush=True)

    def verify_catalogue(self):
        # Build and exercise the full retained browser index before public cutover.
        source_url=self.baseline['status']['url']
        with urlopen(source_url+'/api/collection/catalogue',timeout=60) as response:
            source=json.load(response)
        with urlopen(self.tagged+'/api/instruments?page=0&include_quotes=false',timeout=180) as response:
            merged=json.load(response)
        if (merged.get('catalogue_sha256')!=source.get('sha256') or
            merged.get('catalogue_rows')!=source.get('rows') or not merged.get('items')):
            raise RuntimeError('FULL_RETAINED_CATALOGUE_NOT_VERIFIED')
        print('MERGED_INSTRUMENT_BROWSER_VERIFIED',merged['catalogue_rows'],flush=True)

    def promote(self):
        if self.traffic(self.service())!=self.traffic(self.baseline):
            raise RuntimeError('FRONTEND_TRAFFIC_CHANGED_DURING_HANDOVER')
        self.cloud('run','services','update-traffic','theta-plus','--region='+self.region,
                   '--to-revisions='+self.candidate+'=100','--quiet',capture=True)
        self.promoted=True
        url=self.baseline['status']['url']
        for _ in range(30):
            try:
                with urlopen(url+'/api/v2/system',timeout=15) as response:
                    if json.load(response).get('owner',{}).get('state')=='NEW_BACKEND':break
            except Exception:pass
            time.sleep(2)
        else:raise RuntimeError('PUBLIC_URL_NEW_OWNER_NOT_VERIFIED')
        print('SAME_HTTPS_URL_UNIFIED_BACKEND_VERIFIED',url,flush=True)

    def rollback(self):
        if self.promoted:
            if self.traffic(self.service())!={self.candidate:100}:
                raise RuntimeError('FRONTEND_TRAFFIC_CHANGED; operator rollback review required')
            mapping=','.join(name+'='+str(percent) for name,percent in self.traffic(self.baseline).items())
            self.cloud('run','services','update-traffic','theta-plus','--region='+self.region,
                       '--to-revisions='+mapping,'--quiet',capture=True)
        if self.template_changed:
            current=self.service()
            if any(t.get('tag')=='unified-check' and t.get('revisionName')==self.candidate for t in current['status']['traffic']):
                self.cloud('run','services','update-traffic','theta-plus','--region='+self.region,'--remove-tags=unified-check','--quiet',capture=True)
            # A retry starts with the already verified same private configuration.
            # Preserve that baseline; clear only settings introduced by this run.
            if not self.reused_network:
                self.cloud('run','services','update','theta-plus','--region='+self.region,'--clear-network',
                    '--remove-env-vars=THETA_UNIFIED_UPSTREAM','--no-traffic','--quiet',capture=True)
