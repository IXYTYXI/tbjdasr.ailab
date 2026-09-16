from pathlib import Path
from unittest.mock import Mock
import threading
import time

from fastapi.testclient import TestClient
from app.api import create_app
from app.worker import Worker
from test_pipeline import settings


def add(db, id, room='live/main', start=1):
    db.add_job(dict(id=id, room=room, path=id + '.wav', start=start, duration=1, provider='company'))


def test_worker_automatically_writes_separate_transcripts_and_recovers(tmp_path):
    worker = Worker(settings(tmp_path))
    add(worker.db, 'last', start=3)
    add(worker.db, 'first', start=1)
    add(worker.db, 'other', room='live/other')
    worker.db.update('first', state='succeeded', text='第一句')
    worker.db.update('last', state='failed', text='')
    worker.db.update('other', state='succeeded', text='另一个直播间')
    worker.company = Mock()
    worker.company.submit.side_effect = TimeoutError()
    worker.reconcile = Mock()
    thread = threading.Thread(target=worker.run)
    thread.start()
    main = tmp_path / 'transcripts/live/main.txt'
    other = tmp_path / 'transcripts/live/other.txt'
    try:
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline and not (main.exists() and other.exists()):
            time.sleep(.05)
        assert main.exists(), 'worker must produce readable text automatically'
        text = main.read_text()
        assert '第一句' in text and 'failed' in text and '另一个直播间' not in text
        assert '另一个直播间' in other.read_text()
        worker.db.update('last', state='succeeded', text='最后一句')
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline and '最后一句' not in main.read_text():
            time.sleep(.05)
        text = main.read_text()
        assert text.index('第一句') < text.index('最后一句')
        assert text.count('最后一句') == 1 and 'failed' not in text
    finally:
        worker.stop.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    main.unlink()
    restored = Worker(settings(tmp_path))
    restored.publish_transcripts()
    assert main.read_text() == text


def test_text_download_authenticated_filtered_and_not_html(tmp_path):
    worker = Worker(settings(tmp_path))
    add(worker.db, 'one', start=1)
    add(worker.db, 'two', start=10)
    add(worker.db, 'other', room='live/other')
    for id, text in [('one', '<第一句>'), ('two', '第二句'), ('other', '别的直播间')]:
        worker.db.update(id, state='succeeded', text=text)
    client = TestClient(create_app(worker.cfg))
    assert client.get('/v1/transcript.txt').status_code == 401
    response = client.get('/v1/transcript.txt', params={'room': 'live/main', 'since': 0, 'until': 5},
                          headers={'Authorization': 'Bearer ' + worker.cfg.api_key})
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/plain')
    assert '<第一句>' in response.text
    assert '第二句' not in response.text and '别的直播间' not in response.text


def test_atomic_export_preserves_previous_file_on_failure(tmp_path, monkeypatch):
    from app.documents import save_transcript
    row = dict(start=1, duration=1, provider='company', state='succeeded', text='之前的结果')
    save_transcript(tmp_path, 'live/main', [row])
    target = tmp_path / 'transcripts/live/main.txt'
    old = target.read_bytes()
    def broken_replace(*args):
        raise OSError('simulated disk error')
    monkeypatch.setattr(Path, 'replace', broken_replace)
    import pytest
    with pytest.raises(OSError):
        save_transcript(tmp_path, 'live/main', [dict(row, text='新的结果')])
    assert target.read_bytes() == old
    assert not target.with_suffix('.tmp').exists()
    with pytest.raises(ValueError):
        save_transcript(tmp_path, '../outside', [row])


def test_saved_transcript_not_truncated_to_default_page_size(tmp_path):
    worker = Worker(settings(tmp_path))
    with worker.db.connect() as db:
        db.executemany('''INSERT INTO jobs(id,room,path,start,duration,provider,remote_id,created,state,text)
                          VALUES(?, 'live/main', 'a.wav', ?, 1, 'company', ?, 0, 'succeeded', ?)''',
                       [(str(i), i, str(i), f'句子{i}') for i in range(1005)])
    worker.publish_transcripts()
    text = (tmp_path / 'transcripts/live/main.txt').read_text()
    assert text.count('句子') == 1005
    assert '句子1004' in text
