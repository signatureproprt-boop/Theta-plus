"""SQLite persistence. Independent connections; atomic writes and bounded reads."""
import json
import sqlite3
import shutil
from pathlib import Path
from datetime import datetime, timezone

class Store:
    def __init__(self, path):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.execute('CREATE TABLE IF NOT EXISTS records (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL)')
            c.execute('CREATE INDEX IF NOT EXISTS record_kind ON records(kind,id)')
            c.execute('CREATE TABLE IF NOT EXISTS journal (plan_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL)')
            c.execute('CREATE TABLE IF NOT EXISTS documents (name TEXT PRIMARY KEY, version INTEGER NOT NULL, payload TEXT NOT NULL)')

    def document(self, name):
        with self.connect() as c:
            row=c.execute('SELECT version,payload FROM documents WHERE name=?',(name,)).fetchone()
        return (json.loads(row['payload']),row['version']) if row else (None,0)

    def put_document(self, name, payload, version):
        """Compare-and-swap; version zero creates a previously absent document."""
        encoded=json.dumps(payload,allow_nan=False)
        with self.connect() as c:
            if version==0:
                return c.execute('INSERT OR IGNORE INTO documents VALUES(?,1,?)',(name,encoded)).rowcount==1
            return c.execute('UPDATE documents SET version=version+1,payload=? WHERE name=? AND version=?',
                             (encoded,name,version)).rowcount==1

    def document_names(self, prefix, after='', limit=100):
        with self.connect() as c:
            return [r[0] for r in c.execute('SELECT name FROM documents WHERE name>=? AND name<? AND name>? ORDER BY name LIMIT ?',
                (prefix,prefix+'\uffff',after,limit))]

    def put_catalogue_source(self, sha, source):
        if len(sha)!=64 or any(c not in '0123456789abcdef' for c in sha):raise ValueError('Invalid checksum')
        dest=self.path.parent/'catalogue-sources'/f'{sha}.csv'
        dest.parent.mkdir(parents=True,exist_ok=True)
        # Source archive is immutable and independent of the mutable job manifests.
        import os,tempfile
        fd,temp=tempfile.mkstemp(dir=dest.parent)
        try:
            with os.fdopen(fd,'wb') as out,open(source,'rb') as inp:shutil.copyfileobj(inp,out)
            try:os.link(temp,dest)
            except FileExistsError:pass
        finally:os.unlink(temp)

    def connect(self):
        c=sqlite3.connect(self.path,timeout=15)
        c.row_factory=sqlite3.Row
        return c

    def add(self,kind,payload):
        with self.connect() as c:
            cur=c.execute('INSERT INTO records(kind,created_at,payload) VALUES(?,?,?)',
                          (kind,datetime.now(timezone.utc).isoformat(),json.dumps(payload,allow_nan=False)))
            return cur.lastrowid

    def get(self,kind,id):
        with self.connect() as c:
            row=c.execute('SELECT * FROM records WHERE kind=? AND id=?',(kind,id)).fetchone()
        return self.decode(row) if row else None

    def rows(self,kind,limit=100):
        with self.connect() as c:
            rows=c.execute('SELECT * FROM records WHERE kind=? ORDER BY id DESC LIMIT ?', (kind,limit)).fetchall()
        return [self.decode(r) for r in rows]

    @staticmethod
    def decode(r):
        return {'id':r['id'],'created_at':r['created_at'],'data':json.loads(r['payload'])}

    def journal_add(self,plan_id,payload):
        with self.connect() as c:
            cur=c.execute('INSERT OR IGNORE INTO journal VALUES(?,?,?)',
                         (plan_id,datetime.now(timezone.utc).isoformat(),json.dumps(payload,allow_nan=False)))
            return cur.rowcount == 1

    def journal_rows(self):
        with self.connect() as c:
            rows=c.execute('SELECT * FROM journal ORDER BY created_at DESC LIMIT 1000').fetchall()
        return [{'plan_id':r['plan_id'],'created_at':r['created_at'],**json.loads(r['payload'])} for r in rows]
