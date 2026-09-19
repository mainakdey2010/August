"""PostgreSQL durability in cloud; explicit SQLite path for local development/tests."""
from __future__ import annotations
import json
import os
import sqlite3
import time
from contextlib import contextmanager


def dumps(value):
    return json.dumps(value, allow_nan=False, separators=(',', ':'))


class Store:
    def __init__(self, url=None):
        self.url = url or os.environ.get('DATABASE_URL')
        if not self.url:
            raise RuntimeError('DATABASE_URL is required (Postgres in cloud; sqlite:///path locally)')
        self.sqlite = self.url.startswith('sqlite:///')
        if self.sqlite and os.environ.get('K_SERVICE'):
            raise RuntimeError('Ephemeral SQLite is not supported on Cloud Run')

    @contextmanager
    def connection(self):
        if self.sqlite:
            conn = sqlite3.connect(self.url.removeprefix('sqlite:///'), timeout=30)
        else:
            import psycopg2
            conn = psycopg2.connect(self.url, connect_timeout=10)
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def execute(self, conn, sql, args=()):
        cur = conn.cursor()
        cur.execute(sql if self.sqlite else sql.replace('?', '%s'), args)
        return cur

    def migrate(self):
        with self.connection() as c:
            self.execute(c, 'CREATE TABLE IF NOT EXISTS sera_demo_regions (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
            self.execute(c, '''CREATE TABLE IF NOT EXISTS sera_demo_scans (
                id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL,
                stage TEXT NOT NULL, updated DOUBLE PRECISION NOT NULL,
                config TEXT NOT NULL, result TEXT, error TEXT)''')
            self.execute(c, 'CREATE INDEX IF NOT EXISTS sera_demo_fingerprint ON sera_demo_scans(fingerprint)')

    def region(self, region):
        with self.connection() as c:
            self.execute(c, '''INSERT INTO sera_demo_regions(id,payload) VALUES(?,?)
                ON CONFLICT(id) DO UPDATE SET payload=excluded.payload''',
                (region.region_id, region.model_dump_json()))

    def regions(self):
        with self.connection() as c:
            return [json.loads(r[0]) for r in self.execute(c, 'SELECT payload FROM sera_demo_regions ORDER BY id').fetchall()]

    def create(self, scan_id, fingerprint, config):
        with self.connection() as c:
            # Transaction-scoped advisory lock serializes identical submits across API instances.
            if not self.sqlite:
                self.execute(c, 'SELECT pg_advisory_xact_lock(hashtext(?))', ('sera-demo-dispatch',))
            else:
                self.execute(c, 'BEGIN IMMEDIATE')
            row = self.execute(c, '''SELECT id FROM sera_demo_scans WHERE fingerprint=?
                ORDER BY updated DESC LIMIT 1''', (fingerprint,)).fetchone()
            if row:
                return row[0], False
            active=self.execute(c, "SELECT COUNT(*) FROM sera_demo_scans WHERE status IN ('pending','running') AND updated > ?", (time.time()-2400,)).fetchone()[0]
            if active >= 2:
                raise RuntimeError('DEMO_CAPACITY: two scans are already pending or running')
            self.execute(c, '''INSERT INTO sera_demo_scans
                (id,fingerprint,status,stage,updated,config) VALUES(?,?,?,?,?,?)''',
                (scan_id, fingerprint, 'pending', 'dispatch', time.time(), dumps(config)))
            return scan_id, True

    def get(self, scan_id):
        with self.connection() as c:
            row = self.execute(c, 'SELECT id,status,stage,updated,config,result,error FROM sera_demo_scans WHERE id=?', (scan_id,)).fetchone()
        if not row:
            return None
        keys = ['scan_id','status','stage','updated_at_epoch','config','result','error']
        out = dict(zip(keys,row))
        for key in ['config','result']:
            out[key] = json.loads(out[key]) if out[key] else None
        out['retryable'] = out['status'] in ('failed','dispatch_failed','pending') or (
            out['status'] == 'running' and out['updated_at_epoch'] < time.time()-2400)
        return out

    def claim(self, scan_id):
        with self.connection() as c:
            cur = self.execute(c, '''UPDATE sera_demo_scans SET status='running',stage='imagery',updated=?,error=NULL
                WHERE id=? AND (status IN ('pending','failed','dispatch_failed') OR
                (status='running' AND updated < ?))''', (time.time(), scan_id, time.time()-2400))
            return cur.rowcount == 1

    def update(self, scan_id, status, stage, result=None, error=None):
        with self.connection() as c:
            self.execute(c, '''UPDATE sera_demo_scans SET status=?,stage=?,updated=?,
                result=COALESCE(?,result),error=? WHERE id=?''',
                (status,stage,time.time(),dumps(result) if result is not None else None,error,scan_id))

    def dispatch_failed(self, scan_id):
        with self.connection() as c:
            self.execute(c, "UPDATE sera_demo_scans SET status='dispatch_failed',error=? WHERE id=? AND status='pending'",
                         ('Worker dispatch failed; retry this scan after checking service logs.',scan_id))

    def prepare_retry(self, scan_id):
        with self.connection() as c:
            cur=self.execute(c, """UPDATE sera_demo_scans SET status='pending',stage='dispatch',error=NULL,updated=?
                WHERE id=? AND (status IN ('failed','dispatch_failed','pending') OR
                (status='running' AND updated < ?))""", (time.time(),scan_id,time.time()-2400))
            return cur.rowcount == 1
