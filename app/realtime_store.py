"""Preview text is separate from authoritative archived jobs/documents."""
import time


class PreviewStore:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS realtime_segments (
                id TEXT PRIMARY KEY,room TEXT NOT NULL,start REAL NOT NULL,
                updated REAL NOT NULL,text TEXT NOT NULL,final INTEGER NOT NULL,
                error TEXT NOT NULL DEFAULT '');
                CREATE INDEX IF NOT EXISTS realtime_room_start ON realtime_segments(room,start);''')

    def put(self, stream_id, room, start, text, final, error=''):
        with self.store.connect() as db:
            db.execute('''INSERT INTO realtime_segments VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET updated=excluded.updated,text=excluded.text,
                final=excluded.final,error=excluded.error''',
                (stream_id,room,start,time.time(),text,int(final),error))

    def list(self, room, since=0, limit=100):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute('''SELECT * FROM (
                SELECT * FROM realtime_segments WHERE room=? AND start>=?
                ORDER BY start DESC,id DESC LIMIT ?) ORDER BY start,id''',(room,since,limit))]

    def expire(self, before):
        with self.store.connect() as db:
            db.execute('DELETE FROM realtime_segments WHERE start<?',(before,))

    def interrupt_open(self):
        with self.store.connect() as db:
            db.execute("UPDATE realtime_segments SET error='Realtime process restarted' WHERE final=0 AND error='' ")
