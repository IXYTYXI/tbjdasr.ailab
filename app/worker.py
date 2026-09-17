import fcntl
import hashlib
import json
import logging
import signal
import threading
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import httpx
from .audio import extract_audio, split_wav
from .config import Settings
from .providers import Company, Feishu, ProviderError
from .security import sign
from .store import Store
from .documents import save_transcript

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, cfg):
        self.cfg = cfg
        cfg.validate()
        self.db = Store(cfg.data / 'state.sqlite')
        self.company = Company(cfg.company_url, cfg.company_host, cfg.company_uid, cfg.hotwords)
        self.feishu = Feishu(cfg.app_id, cfg.app_secret)
        self.stop = threading.Event()

    def discover(self):
        root = self.cfg.data / 'recordings'
        for marker in root.rglob('*.ready.json'):
            asset_id = hashlib.sha256(str(marker.relative_to(root)).encode()).hexdigest()
            row = self.db.asset(asset_id, str(marker))
            if row['state'] in ('done', 'failed'):
                continue
            try:
                info = json.loads(marker.read_text())
                if self.cfg.asr_rooms and info['room'] not in self.cfg.asr_rooms:
                    if row['state'] != 'ignored':
                        self.db.asset(asset_id, state='ignored')
                    continue
                source = Path(info['path']).resolve()
                source.relative_to(root.resolve())
                if not source.is_file() or source.suffix != '.mp4':
                    raise ValueError('Recording file missing or not MP4')
                # recordPath uses %s-%f, so timestamps are UTC epoch + microseconds.
                seconds, micros = source.stem.split('-')
                started = int(seconds) + int(micros) / 1000000
                folder = self.cfg.data / 'audio' / asset_id
                folder.mkdir(parents=True, exist_ok=True)
                whole = folder / 'source.wav'
                extract_audio(source, whole)
                parts = split_wav(whole, folder / 'parts', 45)
                for index, (path, offset, duration) in enumerate(parts):
                    self.db.add_job(dict(id=f'{asset_id}_{index:05d}', room=info['room'],
                                        path=str(path.relative_to(self.cfg.data)), start=started + offset,
                                        duration=duration, provider=self.cfg.provider))
                whole.unlink(missing_ok=True)
                self.db.asset(asset_id, state='done')
            except Exception as exc:
                self.db.asset(asset_id, state='failed', error=str(exc)[:2000])
                log.error('Audio extraction failed for asset %s: %s', asset_id, type(exc).__name__)

    def reconcile(self):
        """Recover lost hooks only after authoritative MediaMTX state says a file is closed."""
        if not self.cfg.media_api_password:
            return
        root = self.cfg.data / 'recordings'
        # Snapshot first: a publisher that starts after the API read cannot add a candidate.
        by_room = {}
        for source in root.rglob('*.mp4'):
            by_room.setdefault(str(source.parent.relative_to(root)), []).append(source)
        response = httpx.get(self.cfg.media_api_url.rstrip('/') + '/v3/paths/list',
                             params={'itemsPerPage': 1000}, auth=('worker', self.cfg.media_api_password), timeout=5)
        response.raise_for_status()
        data = response.json()
        if data.get('pageCount', 1) > 1:
            raise ValueError('Too many media paths for reconciliation; increase pagination support')
        active = set()
        for item in data['items']:
            # Unknown API shape is treated as active, never as permission to recover.
            if item.get('online', item.get('ready', True)):
                active.add(item['name'])
        for room, files in by_room.items():
            latest = max(files, key=lambda p: p.name)
            for source in files:
                marker = source.with_suffix('.ready.json')
                if marker.exists() or time.time() - source.stat().st_mtime < self.cfg.orphan_grace:
                    continue
                if room in active and source == latest:
                    continue
                # Earlier segments are closed when the next starts; newest requires offline state.
                temp = marker.with_suffix('.reconcile.tmp')
                temp.write_text(json.dumps({'path': str(source.resolve()), 'room': room, 'recovered': True}))
                temp.replace(marker)

    def step(self, job):
        try:
            if job['provider'] == 'company':
                if job['state'] == 'queued':
                    url = job['audio_url']
                    if not url:
                        expiry = int(time.time()) + self.cfg.url_ttl
                        url = f'{self.cfg.public_url}/audio/{job["id"]}.wav?expires={expiry}&signature={sign(self.cfg.signing_key, job["id"], expiry)}'
                        # Persist BEFORE submit: a lost response must retry the same task + URL.
                        self.db.update(job['id'], audio_url=url)
                    if time.time() >= int(parse_qs(urlparse(url).query)['expires'][0]):
                        raise ProviderError('Audio URL expired; retry with new_remote_task=true')
                    if job['request_json']:
                        payload = json.loads(job['request_json'])
                    else:
                        request = {'model_name': 'qwen3'}
                        if self.cfg.hotwords:
                            request['hotwords'] = self.cfg.hotwords
                        payload = {'user': {'uid': self.cfg.company_uid}, 'audio': {'url': url}, 'request': request}
                        self.db.update(job['id'], request_json=json.dumps(payload, ensure_ascii=False))
                    if not job['submitted_at']:
                        self.db.update(job['id'], submitted_at=time.time())
                    self.company.submit(job['remote_id'], url, payload)
                    self.db.update(job['id'], state='polling', errors=0, error='', next_at=time.time() + self.cfg.poll_seconds)
                    return
                if time.time() > job['submitted_at'] + self.cfg.task_timeout:
                    raise ProviderError('ASR polling exceeded one day; inspect provider and retry')
                result = self.company.poll(job['remote_id'])
                if result is None:
                    self.db.update(job['id'], next_at=time.time() + self.cfg.poll_seconds, errors=0, error='')
                    return
            else:
                result = self.feishu.transcribe(job['id'], self.cfg.data / job['path'])
            self.db.update(job['id'], state='succeeded', text=result['text'], raw=json.dumps(result['raw'], ensure_ascii=False), error='', errors=0)
        except Exception as exc:
            retry = isinstance(exc, (httpx.TransportError, TimeoutError)) or isinstance(exc, ProviderError) and exc.retryable
            errors = job['errors'] + 1
            # Exception URLs may contain signed credentials: only controlled errors are persisted.
            message = str(exc) if isinstance(exc, ProviderError) else type(exc).__name__
            self.db.update(job['id'], state=job['state'] if retry and errors < self.cfg.max_errors else 'failed',
                           resume_state=job['state'], errors=errors, error=message, next_at=time.time() + min(300, 2 ** errors))
            log.warning('ASR task %s: %s', job['id'], message)

    def ingest_loop(self):
        while not self.stop.is_set():
            try:
                try:
                    self.reconcile()
                    self.db.heartbeat('reconcile', {'at': time.time(), 'ok': True})
                except Exception as exc:
                    self.db.heartbeat('reconcile', {'at': time.time(), 'ok': False, 'error': type(exc).__name__})
                self.discover()
                self.db.heartbeat('ingest', {'at': time.time(), 'ok': True})
            except Exception as exc:
                log.error('Ingest loop failed: %s', type(exc).__name__)
            self.stop.wait(2)

    def publish_transcripts(self):
        for room in self.db.rooms(self.cfg.rooms):
            if not room['jobs']:
                continue
            with self.db.connect() as db:
                # Stream a consistent SELECT snapshot without pagination or truncation.
                rows = db.execute('''SELECT start,duration,provider,state,text FROM jobs
                                     WHERE room=? ORDER BY start,id''', (room['room'],))
                save_transcript(self.cfg.data, room['room'], rows)

    def document_loop(self):
        while not self.stop.is_set():
            try:
                self.publish_transcripts()
                self.db.heartbeat('documents', {'at': time.time(), 'ok': True})
            except Exception as exc:
                self.db.heartbeat('documents', {'at': time.time(), 'ok': False, 'error': type(exc).__name__})
                log.error('Transcript export failed: %s', type(exc).__name__)
            self.stop.wait(2)

    def run(self):
        ingest = threading.Thread(target=self.ingest_loop)
        documents = threading.Thread(target=self.document_loop)
        ingest.start()
        documents.start()
        try:
            while not self.stop.is_set():
                self.db.heartbeat('asr', {'at': time.time(), 'ok': True})
                for job in self.db.due(rooms=self.cfg.asr_rooms or None):
                    if self.stop.is_set():
                        break
                    self.step(job)
                self.stop.wait(1)
        finally:
            self.stop.set()
            ingest.join()
            documents.join()
            self.publish_transcripts()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    worker = Worker(Settings())
    # One worker owns this SQLite queue. Scaling requires an explicit lease design.
    with (worker.cfg.data / 'worker.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: worker.stop.set())
        worker.run()
