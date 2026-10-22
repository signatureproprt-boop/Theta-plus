"""Bounded failure evidence without exception messages, locals, URLs or secrets."""
import hashlib
import json
from pathlib import Path
import re
import traceback
from collection import now,replace

KEY='system/diagnostics'


def fingerprint(stage,error,job_id=None):
    def leaf(exc):
        frames=[]
        for frame in traceback.extract_tb(exc.__traceback__)[-6:]:
            filename=Path(frame.filename).name
            function=frame.name
            frames.append({'file':filename if re.fullmatch(r'[A-Za-z0-9_.-]{1,100}\.py',filename) else '<code>',
                           'line':frame.lineno,'function':function if re.fullmatch(r'[A-Za-z0-9_<>]{1,100}',function) else '<function>'})
        entry={'exception_type':type(exc).__name__[:80],'frames':frames}
        status=getattr(exc,'status_code',None) or getattr(exc,'status',None)
        if type(status) is not int:status=getattr(getattr(exc,'response',None),'status_code',None)
        if type(status) is int and 100<=status<=599:entry['http_status']=status
        code=getattr(exc,'code',None)
        if isinstance(code,str) and re.fullmatch(r'(DH-[0-9]{3}|[0-9]{3}|BUDGET|TIMEOUT|NETWORK|ENDPOINT|PAYLOAD)',code):entry['provider_code']=code
        return entry
    leaves=[]
    def visit(exc):
        if len(leaves)>=8:return
        if isinstance(exc,BaseExceptionGroup):
            for child in exc.exceptions:visit(child)
        else:leaves.append(leaf(exc))
    visit(error)
    record={'stage':stage if re.fullmatch(r'[a-z0-9_-]{1,64}',stage) else 'worker',
            'exception_type':type(error).__name__[:80],'causes':leaves}
    if isinstance(job_id,str) and re.fullmatch(r'[a-f0-9]{32}',job_id):record['job_id']=job_id
    record['id']=hashlib.sha256(json.dumps(record,sort_keys=True).encode()).hexdigest()[:16]
    return record


def capture(store,stage,error,job_id=None):
    record={**fingerprint(stage,error,job_id),'last_at':now()}
    # JSON contains only code locations and allowlisted status fields. Never
    # stringify the exception or include traceback source lines / local values.
    print('THETA_DIAGNOSTIC '+json.dumps(record,separators=(',',':')),flush=True)
    def save(old):
        old=old or {};items=list(old.get('items',[]));previous=next((i for i in items if i.get('id')==record['id']),{})
        item={**record,'first_at':previous.get('first_at',record['last_at']),
              'occurrences':previous.get('occurrences',0)+1}
        return {'updated_at':record['last_at'],'total':old.get('total',0)+1,
                'items':[item]+[i for i in items if i.get('id')!=item['id']][:19]}
    try:replace(store,KEY,save)
    except Exception:print('THETA_DIAGNOSTIC_STORE_UNAVAILABLE id='+record['id'],flush=True)
    return record
