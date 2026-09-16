import json
import secrets
import shutil
import time
import uuid
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Depends, Query
from fastapi.responses import FileResponse, Response
from .config import Settings
from .documents import render_xml
from .security import verify
from .store import Store


def create_app(cfg=None):
    cfg = cfg or Settings()
    cfg.validate()
    db = Store(cfg.data / 'state.sqlite')
    app = FastAPI(title='直播收流与语音转写', version='1.0.0', docs_url=None, redoc_url=None, openapi_url=None)

    def authorize(authorization: Annotated[str | None, Header()] = None):
        if not secrets.compare_digest(authorization or '', 'Bearer ' + cfg.api_key):
            raise HTTPException(401, 'API key required')

    guard = [Depends(authorize)]

    @app.get('/v1/rooms', dependencies=guard)
    def rooms():
        # Configured is an allowlist flag, not a publisher-online signal.
        return db.rooms(cfg.rooms)

    @app.get('/healthz')
    def health():
        return {'status': 'ok'}

    @app.get('/v1/status', dependencies=guard)
    def status():
        info = db.summary()
        disk = shutil.disk_usage(cfg.data)
        info.update(provider_for_new_jobs=cfg.provider, disk_free_bytes=disk.free,
                    disk_low=disk.free < 5 * 1024 ** 3,
                    worker_stale=any(time.time() - info['worker'].get(k, {}).get('at', 0) > 180 for k in ('ingest', 'asr')))
        return info

    @app.get('/v1/jobs', dependencies=guard)
    def jobs(room: str | None = None, limit: int = Query(100, ge=1, le=1000),
             offset: int = Query(0, ge=0), since: float = 0, until: float = 1e20):
        # Presigned URL and provider request payload are not returned by list endpoints.
        return [{k: v for k, v in r.items() if k not in ('audio_url', 'raw', 'path', 'request_json')} for r in db.list_jobs(room, limit, since, until, offset)]

    @app.get('/v1/jobs/{job_id}', dependencies=guard)
    def job(job_id: str):
        r = db.get(job_id)
        if not r:
            raise HTTPException(404, 'Unknown job')
        r.pop('audio_url')
        r.pop('path')
        r.pop('request_json')
        r['raw'] = json.loads(r['raw'])
        return r

    @app.post('/v1/jobs/{job_id}/retry', dependencies=guard)
    def retry(job_id: str, new_remote_task: bool = False):
        r = db.get(job_id)
        if not r:
            raise HTTPException(404, 'Unknown job')
        if r['state'] != 'failed':
            raise HTTPException(409, 'Only failed jobs can be retried')
        fields = dict(state=r['resume_state'], errors=0, error='', next_at=0)
        if new_remote_task or r['provider'] == 'feishu':
            fields.update(state='queued', remote_id=str(uuid.uuid4()), audio_url='', request_json='', submitted_at=0)
        else:
            # Continue polling the same task; do not extend its original signed URL.
            fields['submitted_at'] = time.time()  # poll timeout restarts; URL itself remains unchanged
        db.update(job_id, **fields)
        return {'ok': True}

    @app.get('/v1/assets', dependencies=guard)
    def assets():
        with db.connect() as conn:
            return [dict(r) for r in conn.execute('SELECT id,state,error,updated FROM assets ORDER BY updated DESC LIMIT 1000')]

    @app.post('/v1/assets/{asset_id}/retry', dependencies=guard)
    def retry_asset(asset_id: str):
        row = db.asset(asset_id)
        if not row:
            raise HTTPException(404, 'Unknown asset')
        if row['state'] != 'failed':
            raise HTTPException(409, 'Only failed assets can be retried')
        db.asset(asset_id, state='queued')
        return {'ok': True}

    @app.get('/v1/transcript.xml', dependencies=guard)
    def transcript(room: str = 'live/main', since: float = 0, until: float = 1e20):
        rows = db.list_jobs(room, 10001, since, until)
        if len(rows) > 10000:
            raise HTTPException(413, 'Select a smaller since/until time range')
        # DocxXML is a fragment by design; root is only used for XML transport.
        content = '<document>' + render_xml(room, rows) + '</document>'
        return Response(content, media_type='application/xml')

    @app.api_route('/audio/{job_id}.wav', methods=['GET', 'HEAD'])
    def audio(job_id: str, expires: int, signature: str):
        if not verify(cfg.signing_key, job_id, expires, signature):
            raise HTTPException(403, 'Expired or invalid audio signature')
        r = db.get(job_id)
        if not r:
            raise HTTPException(404, 'Unknown audio')
        path = (cfg.data / r['path']).resolve()
        if not path.is_relative_to(cfg.data.resolve()) or not path.is_file():
            raise HTTPException(404, 'Audio not available')
        return FileResponse(path, media_type='audio/wav', headers={'Cache-Control': 'private, no-store'})

    return app
