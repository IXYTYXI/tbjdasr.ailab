import json
import time
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from test_service import wav_file


def settings(tmp_path):
    from app.config import Settings
    return Settings(data=tmp_path, api_key='a' * 32, signing_key='b' * 32, public_url='https://audio.example.com')


def test_signed_audio_requires_valid_signature_and_supports_range(tmp_path):
    from app.api import create_app
    from app.store import Store
    from app.security import sign
    cfg = settings(tmp_path)
    db = Store(tmp_path / 'state.sqlite')
    wav_file(tmp_path / 'a.wav')
    db.add_job(dict(id='id1', room='live/main', path='a.wav', start=0, duration=1, provider='company'))
    client = TestClient(create_app(cfg))
    assert client.get('/v1/jobs').status_code == 401
    assert client.get('/v1/jobs', headers={'Authorization': 'Bearer ' + cfg.api_key}).status_code == 200
    assert client.get('/audio/id1.wav?expires=1&signature=no').status_code == 403
    expiry = int(time.time()) + 100
    url = f'/audio/id1.wav?expires={expiry}&signature={sign(cfg.signing_key, "id1", expiry)}'
    response = client.get(url, headers={'Range': 'bytes=0-3'})
    assert response.status_code == 206
    assert response.content == b'RIFF'
    head = client.head(url)
    assert head.status_code == 200
    assert int(head.headers['content-length']) > 44
    assert head.content == b''


def test_company_submission_survives_retry_with_same_id_and_url(tmp_path):
    from app.store import Store
    from app.worker import Worker
    cfg = settings(tmp_path)
    db = Store(tmp_path / 'state.sqlite')
    db.add_job(dict(id='j', room='live/main', path='a.wav', start=0, duration=1, provider='company'))
    worker = Worker(cfg)
    worker.company = Mock()
    worker.company.submit.side_effect = TimeoutError('lost response')
    worker.step(db.get('j'))
    first = worker.company.submit.call_args.args
    worker.company.submit.side_effect = None
    worker.step(db.get('j'))
    assert worker.company.submit.call_args.args == first
    assert db.get('j')['state'] == 'polling'
    worker.company.poll.return_value = {'text': '结果', 'raw': {}}
    worker.step(db.get('j'))
    assert db.get('j')['text'] == '结果'


def test_old_queued_job_gets_full_polling_time_and_preserves_payload(tmp_path):
    from app.store import Store
    from app.worker import Worker
    cfg = settings(tmp_path)
    worker = Worker(cfg)
    worker.db.add_job(dict(id='old', room='live/main', path='a.wav', start=0, duration=1, provider='company'))
    worker.db.update('old', created=time.time() - 90000)
    original = worker.company
    original.post = Mock(side_effect=TimeoutError())
    worker.step(worker.db.get('old'))
    first_body = original.post.call_args.args[2]
    original.uid = 'changed'
    original.hotwords = 'changed'
    original.post.side_effect = None
    original.post.return_value = (None, {'code': '20000000'})
    worker.step(worker.db.get('old'))
    assert original.post.call_args.args[2] == first_body
    original.poll = Mock(return_value=None)
    worker.step(worker.db.get('old'))
    original.poll.assert_called_once()


def test_unfinished_media_is_not_ingested_and_duplicate_marker_is_safe(tmp_path, monkeypatch):
    from app.worker import Worker
    from app.store import Store
    cfg = settings(tmp_path)
    worker = Worker(cfg)
    folder = tmp_path / 'recordings' / 'live' / 'main'
    folder.mkdir(parents=True)
    raw = folder / '1700000000-000000.mp4'
    raw.write_bytes(b'unfinished')
    worker.discover()
    assert worker.db.list_jobs() == []
    def extract(source, target):
        wav_file(target, 1)
    monkeypatch.setattr('app.worker.extract_audio', extract)
    marker = raw.with_suffix('.ready.json')
    marker.write_text(json.dumps({'path': str(raw), 'room': 'live/main'}))
    worker.discover()
    worker.discover()
    assert len(worker.db.list_jobs()) == 1


def test_file_outside_recordings_rejected(tmp_path):
    from app.worker import Worker
    cfg = settings(tmp_path)
    folder = tmp_path / 'recordings'
    folder.mkdir()
    marker = folder / 'bad.ready.json'
    marker.write_text(json.dumps({'path': '/etc/passwd', 'room': 'live/main'}))
    worker = Worker(cfg)
    worker.discover()
    assert worker.db.summary()['assets']['failed'] == 1
    assert worker.db.list_jobs() == []
