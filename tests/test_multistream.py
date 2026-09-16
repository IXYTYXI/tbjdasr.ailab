import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.store import Store
from app.api import create_app
from media_entry import configuration


def test_multiple_publish_paths_preserve_existing_room(monkeypatch):
    monkeypatch.setenv('PUBLISH_PASSWORD', 'p' * 32)
    monkeypatch.setenv('STREAM_PATH', 'live/main')
    monkeypatch.setenv('STREAM_PATHS', 'live/taobao, live/jingdong,live/taobao')
    cfg = configuration()
    assert set(cfg['paths']) == {'live/main', 'live/taobao', 'live/jingdong'}
    assert cfg['authInternalUsers'][0]['permissions'] == [
        {'action': 'publish', 'path': room} for room in cfg['paths']]
    assert cfg['pathDefaults']['overridePublisher'] is False


@pytest.mark.parametrize('rooms', ['live/../oops', 'live/*', 'live/a,', '/live/a', 'live/a?pass=x'])
def test_invalid_publish_paths_rejected(monkeypatch, rooms):
    monkeypatch.setenv('PUBLISH_PASSWORD', 'p' * 32)
    monkeypatch.setenv('STREAM_PATHS', rooms)
    with pytest.raises(ValueError, match='STREAM_PATH'):
        configuration()


def add(db, id, room):
    db.add_job(dict(id=id, room=room, path=f'{id}.wav', start=1, duration=1, provider='company'))


def test_busy_room_does_not_fill_entire_asr_batch(tmp_path):
    db = Store(tmp_path / 'db.sqlite')
    for n in range(12):
        add(db, f'a{n}', 'live/a')
    add(db, 'b', 'live/b')
    add(db, 'c', 'live/c')
    assert {row['room'] for row in db.due(limit=3)} == {'live/a', 'live/b', 'live/c'}
    db.update('b', next_at=1e20)
    assert 'b' not in {r['id'] for r in db.due(limit=30)}


def test_room_status_and_transcripts_are_separate(tmp_path, monkeypatch):
    monkeypatch.setenv('STREAM_PATH', 'live/main')
    monkeypatch.setenv('STREAM_PATHS', 'live/a,live/b')
    cfg = Settings(data=tmp_path, api_key='a' * 32, signing_key='b' * 32, public_url='https://example.com')
    db = Store(tmp_path / 'state.sqlite')
    add(db, 'a', 'live/a')
    add(db, 'b', 'live/b')
    db.update('a', state='succeeded', text='淘宝声音')
    db.update('b', state='failed', text='京东声音')
    client = TestClient(create_app(cfg))
    assert client.get('/v1/rooms').status_code == 401
    headers = {'Authorization': 'Bearer ' + cfg.api_key}
    response = client.get('/v1/rooms', headers=headers)
    assert response.status_code == 200
    rooms = {r['room']: r for r in response.json()}
    assert rooms['live/main']['jobs'] == {}
    assert rooms['live/a']['jobs'] == {'succeeded': 1}
    assert rooms['live/b']['jobs'] == {'failed': 1}
    assert all(r['configured'] for r in rooms.values())
    xml = client.get('/v1/transcript.xml', params={'room': 'live/a'}, headers=headers).text
    assert '淘宝声音' in xml and '京东声音' not in xml
    jobs = client.get('/v1/jobs', params={'room': 'live/b'}, headers=headers).json()
    assert [j['id'] for j in jobs] == ['b']


def test_more_rooms_than_batch_size_get_turns(tmp_path):
    db = Store(tmp_path / 'db.sqlite')
    for n in range(12):
        for i in range(3):
            add(db, f'{n}-{i}', f'live/room{n:02d}')
    seen = set()
    for _ in range(4):
        seen.update(j['room'] for j in db.due(limit=3))
    assert len(seen) == 12
