import base64
import json
import wave
from pathlib import Path

import httpx
import pytest


def wav_file(path, seconds=1):
    with wave.open(str(path), 'wb') as f:
        f.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        f.writeframes(b'\x01\x00' * int(16000 * seconds))
    return path


def test_signature_expiry_and_resource_binding():
    from app.security import sign, verify
    sig = sign('secret', 'job-a', 100)
    assert verify('secret', 'job-a', 100, sig, now=99)
    assert not verify('secret', 'job-a', 100, sig, now=101)
    assert not verify('secret', 'job-b', 100, sig, now=99)


def test_audio_split_preserves_last_frames(tmp_path):
    from app.audio import split_wav, read_pcm
    source = wav_file(tmp_path / 'source.wav', 91.01)
    parts = split_wav(source, tmp_path / 'parts', 45)
    assert len(parts) == 3
    assert [p[1] for p in parts] == [0, 45, 90]
    assert b''.join(read_pcm(p[0]) for p in parts) == read_pcm(source, max_seconds=100)
    assert all(p[2] <= 45 for p in parts)


def test_feishu_pcm_removes_wav_header_and_checks_duration(tmp_path):
    from app.audio import read_pcm
    path = wav_file(tmp_path / 'a.wav')
    assert read_pcm(path) == b'\x01\x00' * 16000
    long = wav_file(tmp_path / 'b.wav', 61)
    with pytest.raises(ValueError):
        read_pcm(long)


def test_db_idempotency_and_persisted_remote_state(tmp_path):
    from app.store import Store
    db = Store(tmp_path / 'db.sqlite')
    job = dict(id='a', room='live/main', path='a.wav', start=1.0, duration=2.0, provider='company')
    db.add_job(job)
    db.update('a', remote_id='persist-me', audio_url='https://host/audio', state='polling')
    db.add_job(job)
    other = Store(tmp_path / 'db.sqlite')
    assert other.get('a')['remote_id'] == 'persist-me'
    assert other.get('a')['state'] == 'polling'
    assert len(other.list_jobs()) == 1


def test_company_contract(tmp_path):
    from app.providers import Company
    calls = []
    def handler(request):
        calls.append(request)
        assert request.headers['host'] == 'qwen3-dual-asr.ai'
        assert request.headers['X-Api-Request-Id'] == 'id-1'
        if request.url.path.endswith('/submit'):
            body = json.loads(request.content)
            assert body['audio']['url'] == 'https://our/audio.wav'
            assert body['request']['model_name'] == 'qwen3'
            return httpx.Response(200, json={'code': '20000000', 'status': 'accepted'})
        if request.url.path.endswith('/query'):
            return httpx.Response(200, headers={'X-Api-Status-Code': '20000000'}, json={})
        return httpx.Response(200, json={'result': {'text': '主播讲话', 'utterances': [{'text': '主播讲话'}]}})
    p = Company('http://asr', 'qwen3-dual-asr.ai', 'live', '', httpx.Client(transport=httpx.MockTransport(handler)))
    p.submit('id-1', 'https://our/audio.wav')
    result = p.poll('id-1')
    assert result['text'] == '主播讲话'
    assert len(calls) == 3


def test_company_queue_and_failure():
    from app.providers import Company, ProviderError
    code = ['20000002']
    p = Company('http://asr', '', 'live', '', httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, headers={'X-Api-Status-Code': code[0]}, json={}))))
    assert p.poll('id') is None
    code[0] = '55000031'
    with pytest.raises(ProviderError):
        p.poll('id')


def test_feishu_body_token_cache_and_result(tmp_path):
    from app.providers import Feishu
    seen = []
    def handler(request):
        seen.append(request.url.path)
        if request.url.path.endswith('internal'):
            return httpx.Response(200, json={'code': 0, 'tenant_access_token': 'test-token', 'expire': 7200})
        assert request.headers['authorization'] == 'Bearer test-token'
        data = json.loads(request.content)
        assert data['config']['format'] == 'pcm'
        assert len(data['config']['file_id']) == 16
        assert base64.b64decode(data['speech']['speech']) == b'\x01\x00' * 16000
        return httpx.Response(200, json={'code': 0, 'data': {'recognition_text': '测试'}})
    p = Feishu('app', 'secret', httpx.Client(transport=httpx.MockTransport(handler)))
    path = wav_file(tmp_path / 'a.wav')
    assert p.transcribe('abc', path)['text'] == '测试'
    p.transcribe('abc', path)
    assert len(seen) == 3


def test_document_escapes_and_marks_pending():
    from app.documents import render_xml
    xml = render_xml('live/main', [{'start': 0, 'duration': 2, 'state': 'succeeded', 'text': '<主播>&', 'provider': 'company'},
                                   {'start': 2, 'duration': 2, 'state': 'failed', 'text': '', 'provider': 'company'}])
    assert '&lt;主播&gt;&amp;' in xml
    assert 'failed' in xml
