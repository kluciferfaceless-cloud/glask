"""Shared storage for Glask: a JSON key/value store plus an audit log.

Uses Postgres when DATABASE_URL is a postgres URL (Railway), otherwise a local SQLite file.
"""
import json, os, sqlite3, threading, time

DATABASE_URL = os.environ.get('DATABASE_URL', '')
IS_PG = DATABASE_URL.startswith(('postgres://', 'postgresql://'))

if IS_PG:
    import psycopg
    from psycopg.types.json import Jsonb


class Store:
    def __init__(self):
        self.lock = threading.Lock()
        if IS_PG:
            self.conn = psycopg.connect(DATABASE_URL, autocommit=True)
            with self.conn.cursor() as c:
                c.execute("""create table if not exists kv(
                    key text primary key, value jsonb not null,
                    updated_by text, updated_at double precision)""")
                c.execute("""create table if not exists audit(
                    id bigserial primary key, at double precision, who text, action text, key text, detail text)""")
        else:
            path = os.environ.get('SQLITE_PATH', 'glask.db')
            self.conn = sqlite3.connect(path, check_same_thread=False)
            self.conn.execute("create table if not exists kv(key text primary key, value text not null, updated_by text, updated_at real)")
            self.conn.execute("create table if not exists audit(id integer primary key autoincrement, at real, who text, action text, key text, detail text)")
            self.conn.commit()

    def _q(self, sql, args=(), fetch=False):
        with self.lock:
            if IS_PG:
                try:
                    with self.conn.cursor() as c:
                        c.execute(sql.replace('?', '%s'), args)
                        return c.fetchall() if fetch else None
                except psycopg.OperationalError:
                    # connection dropped (Railway restarts Postgres occasionally): reconnect once
                    self.conn = psycopg.connect(DATABASE_URL, autocommit=True)
                    with self.conn.cursor() as c:
                        c.execute(sql.replace('?', '%s'), args)
                        return c.fetchall() if fetch else None
            cur = self.conn.execute(sql, args)
            rows = cur.fetchall() if fetch else None
            self.conn.commit()
            return rows

    def all(self):
        rows = self._q("select key, value from kv", fetch=True)
        return {k: (v if IS_PG else json.loads(v)) for k, v in rows}

    def get(self, key):
        rows = self._q("select value from kv where key = ?", (key,), fetch=True)
        if not rows:
            return None
        return rows[0][0] if IS_PG else json.loads(rows[0][0])

    def put(self, key, value, who):
        v = Jsonb(value) if IS_PG else json.dumps(value)
        now = time.time()
        if value is None:
            self._q("delete from kv where key = ?", (key,))
        elif IS_PG:
            self._q("insert into kv(key,value,updated_by,updated_at) values (?,?,?,?) "
                    "on conflict(key) do update set value=excluded.value, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                    (key, v, who, now))
        else:
            self._q("insert or replace into kv(key,value,updated_by,updated_at) values (?,?,?,?)", (key, v, who, now))
        self.audit(who, 'delete' if value is None else 'save', key, '')

    def audit(self, who, action, key, detail):
        self._q("insert into audit(at,who,action,key,detail) values (?,?,?,?,?)", (time.time(), who, action, key, detail[:500]))

    def recent_audit(self, n=200):
        return [dict(at=r[0], who=r[1], action=r[2], key=r[3], detail=r[4])
                for r in self._q("select at,who,action,key,detail from audit order by id desc limit ?", (n,), fetch=True)]
