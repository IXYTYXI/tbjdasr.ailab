import json
import sqlite3
import time
import uuid
from contextlib import contextmanager


class Store:
    def __init__(self, path):
        self.path = str(path)
        self._last_scheduled_room = ''
        with self.connect() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY, room TEXT NOT NULL, path TEXT NOT NULL,
              start REAL NOT NULL, duration REAL NOT NULL, provider TEXT NOT NULL,
              state TEXT NOT NULL DEFAULT 'queued', remote_id TEXT NOT NULL,
              audio_url TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
              next_at REAL NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0,
              error TEXT NOT NULL DEFAULT '', text TEXT NOT NULL DEFAULT '', raw TEXT NOT NULL DEFAULT '{}',
              resume_state TEXT NOT NULL DEFAULT 'queued',
              submitted_at REAL NOT NULL DEFAULT 0, request_json TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS job_schedule ON jobs(state,next_at);
            CREATE INDEX IF NOT EXISTS job_room_start ON jobs(room,start);
            CREATE TABLE IF NOT EXISTS assets (
              id TEXT PRIMARY KEY, marker TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
              error TEXT NOT NULL DEFAULT '', updated REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS service_state (key TEXT PRIMARY KEY,value TEXT NOT NULL);
            ''')
            if 'resume_state' not in {r[1] for r in db.execute('PRAGMA table_info(jobs)')}:
                db.execute("ALTER TABLE jobs ADD COLUMN resume_state TEXT NOT NULL DEFAULT 'queued'")
            for name, definition in [('submitted_at', 'REAL NOT NULL DEFAULT 0'), ('request_json', "TEXT NOT NULL DEFAULT ''")]:
                if name not in {r[1] for r in db.execute('PRAGMA table_info(jobs)')}:
                    db.execute(f'ALTER TABLE jobs ADD COLUMN {name} {definition}')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def add_job(self, job):
        with self.connect() as db:
            db.execute('''INSERT OR IGNORE INTO jobs
              (id,room,path,start,duration,provider,remote_id,created) VALUES (?,?,?,?,?,?,?,?)''',
              tuple(job[k] for k in ('id', 'room', 'path', 'start', 'duration', 'provider')) + (str(uuid.uuid4()), time.time()))

    def get(self, job_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            return dict(row) if row else None

    def update(self, job_id, **values):
        allowed = {'state', 'remote_id', 'audio_url', 'next_at', 'errors', 'error', 'text', 'raw', 'created', 'resume_state', 'submitted_at', 'request_json'}
        if not values or not values.keys() <= allowed:
            raise ValueError('Invalid update')
        with self.connect() as db:
            db.execute('UPDATE jobs SET ' + ','.join(f'{k}=?' for k in values) + ' WHERE id=?', (*values.values(), job_id))

    def list_jobs(self, room=None, limit=1000, since=0, until=1e20, offset=0):
        query = 'SELECT * FROM jobs WHERE start>=? AND start<?'
        args = [since, until]
        if room:
            query += ' AND room=?'
            args.append(room)
        with self.connect() as db:
            return [dict(r) for r in db.execute(query + ' ORDER BY start,id LIMIT ? OFFSET ?', (*args, limit, offset))]

    def due(self, limit=8):
        with self.connect() as db:
            rows = db.execute('''SELECT * FROM (
                SELECT *, ROW_NUMBER() OVER (PARTITION BY room ORDER BY next_at,created,id) AS room_rank
                FROM jobs WHERE state IN ('queued','polling') AND next_at<=?
                ) ORDER BY room_rank,CASE WHEN room>? THEN 0 ELSE 1 END,room LIMIT ?''',
                (time.time(), self._last_scheduled_room, limit))
            jobs = [{k: r[k] for k in r.keys() if k != 'room_rank'} for r in rows]
            if jobs:
                self._last_scheduled_room = jobs[-1]['room']
            return jobs

    def rooms(self, configured):
        result = {room: {'room': room, 'configured': True, 'jobs': {}} for room in configured}
        with self.connect() as db:
            for row in db.execute('SELECT room,state,COUNT(*) AS count FROM jobs GROUP BY room,state'):
                info = result.setdefault(row['room'], {'room': row['room'], 'configured': False, 'jobs': {}})
                info['jobs'][row['state']] = row['count']
        return [result[room] for room in sorted(result)]

    def asset(self, asset_id, marker=None, state=None, error=''):
        with self.connect() as db:
            if marker is not None:
                db.execute('INSERT OR IGNORE INTO assets(id,marker,updated) VALUES(?,?,?)', (asset_id, marker, time.time()))
            if state:
                db.execute('UPDATE assets SET state=?,error=?,updated=? WHERE id=?', (state, error, time.time(), asset_id))
            row = db.execute('SELECT * FROM assets WHERE id=?', (asset_id,)).fetchone()
            return dict(row) if row else None

    def summary(self):
        with self.connect() as db:
            return dict(jobs={r[0]: r[1] for r in db.execute('SELECT state,COUNT(*) FROM jobs GROUP BY state')},
                        assets={r[0]: r[1] for r in db.execute('SELECT state,COUNT(*) FROM assets GROUP BY state')},
                        worker={r[0]: json.loads(r[1]) for r in db.execute('SELECT key,value FROM service_state')})

    def heartbeat(self, key, value):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO service_state VALUES (?,?)', (key, json.dumps(value)))
