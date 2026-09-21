import base64
import json
import httpx
import pytest


def provider(responses):
    from app.realtime import StreamRecognizer
    from app.providers import Feishu
    calls=[]
    def handle(request):
        body=json.loads(request.content);calls.append(body)
        response=responses.pop(0)
        return httpx.Response(200,json={'code':0,'data':dict(stream_id=body['config']['stream_id'],sequence_id=body['config']['sequence_id'],recognition_text=response)})
    p=Feishu('a','b',httpx.Client(transport=httpx.MockTransport(handle)))
    p.token='test';p.expires=1e20
    return StreamRecognizer(p,stream_id='0123456789abcdef'),calls


def test_stream_protocol_and_result_replacement():
    p,calls=provider(['你好','你好世界','你好世界。'])
    assert p.send(b'\x00\x00'*3200)=='你好'
    assert p.send(b'\x01\x00'*3200)=='你好世界'
    assert p.finish()=='你好世界。'
    assert [x['config']['action'] for x in calls]==[1,0,2]
    assert [x['config']['sequence_id'] for x in calls]==[0,1,2]
    assert base64.b64decode(calls[0]['speech']['speech'])==b'\x00\x00'*3200
    with pytest.raises(ValueError):p.send(b'\x00\x00')


def test_invalid_pcm_rejected_before_request():
    p,calls=provider([])
    for pcm in [b'',b'x',b'\x00'*6402]:
        with pytest.raises(ValueError):p.send(pcm)
    assert calls==[]


def test_uncertain_request_closes_stream_instead_of_replaying():
    from app.realtime import StreamRecognizer
    from app.providers import Feishu
    def fail(request):raise httpx.ReadTimeout('not logged')
    p=Feishu('a','b',httpx.Client(transport=httpx.MockTransport(fail)))
    p.token='test';p.expires=1e20
    stream=StreamRecognizer(p)
    with pytest.raises(httpx.ReadTimeout):stream.send(b'\x00'*6400)
    with pytest.raises(ValueError):stream.send(b'\x00'*6400)


def test_preview_store_replaces_partial_and_separates_archive(tmp_path):
    from app.store import Store
    from app.realtime_store import PreviewStore
    db=Store(tmp_path/'state.sqlite');preview=PreviewStore(db)
    preview.put('stream1','live/taobao',10,'你',False)
    preview.put('stream1','live/taobao',10,'你好',False)
    preview.put('stream1','live/taobao',10,'你好。',True)
    rows=preview.list('live/taobao')
    assert len(rows)==1 and rows[0]['text']=='你好。' and rows[0]['final']
    assert db.list_jobs()==[]
    assert preview.list('live/jingdong')==[]


def test_preview_api_auth_filter_and_staleness(tmp_path):
    from app.api import create_app
    from app.config import Settings
    from app.store import Store
    from app.realtime_store import PreviewStore
    from fastapi.testclient import TestClient
    cfg=Settings(data=tmp_path,rooms=('live/taobao',),api_key='a'*32,signing_key='b'*32,provider='feishu')
    db=Store(tmp_path/'state.sqlite');PreviewStore(db).put('s','live/taobao',10,'测试',False)
    c=TestClient(create_app(cfg))
    assert c.get('/v1/realtime',params={'room':'live/taobao'}).status_code==401
    r=c.get('/v1/realtime',params={'room':'live/taobao'},headers={'Authorization':'Bearer '+'a'*32})
    assert r.status_code==200 and r.json()['stale']
    assert r.json()['segments'][0]['text']=='测试'


def test_realtime_media_read_access_is_opt_in_and_allowlisted(monkeypatch):
    from media_entry import configuration
    monkeypatch.setenv('PUBLISH_PASSWORD','p'*32)
    monkeypatch.setenv('MEDIA_API_PASSWORD','m'*32)
    monkeypatch.setenv('STREAM_PATH','live/main')
    monkeypatch.setenv('STREAM_PATHS','live/taobao,live/jingdong')
    monkeypatch.setenv('ASR_STREAM_PATHS','live/taobao')
    monkeypatch.setenv('REALTIME_ASR_ENABLED','true')
    cfg=configuration()
    assert cfg['rtsp'] and cfg['rtspTransports']==['tcp']
    user=next(u for u in cfg['authInternalUsers'] if u['user']=='worker')
    assert {'action':'read','path':'live/taobao'} in user['permissions']
    assert {'action':'read','path':'live/main'} not in user['permissions']
    monkeypatch.setenv('REALTIME_ASR_ENABLED','false')
    assert not configuration()['rtsp']


def test_server_prefixed_id_and_lagging_result_sequence():
    from app.realtime import StreamRecognizer
    from app.providers import Feishu
    def handle(request):
        body=json.loads(request.content)
        return httpx.Response(200,json={'code':0,'data':{'stream_id':'tenant_'+body['config']['stream_id'],'sequence_id':0,'recognition_text':'修订'}})
    p=Feishu('a','b',httpx.Client(transport=httpx.MockTransport(handle)));p.token='t';p.expires=1e20
    s=StreamRecognizer(p)
    assert s.send(b'\x00'*6400)=='修订'
    assert s.send(b'\x00'*6400)=='修订'
    assert s.finish()=='修订'


def test_pcm_packetizer_preserves_tail_and_rejects_odd_sample():
    import os,threading
    from app.realtime_worker import pcm_packets
    r,w=os.pipe()
    with os.fdopen(r,'rb',buffering=0) as read:
        os.write(w,b'x'*6500);os.close(w)
        assert [len(p) for p in pcm_packets(read,threading.Event())]==[6400,100]
    r,w=os.pipe()
    with os.fdopen(r,'rb',buffering=0) as read:
        os.write(w,b'x');os.close(w)
        with pytest.raises(ValueError):list(pcm_packets(read,threading.Event()))


def test_capture_rotation_finalizes_tail_without_archive_jobs(tmp_path):
    import threading
    from app.realtime_worker import transcribe_packets
    from app.realtime_store import PreviewStore
    from app.store import Store
    p,_=provider(['一','一二','一二。','三','三。'])
    store=Store(tmp_path/'state.sqlite');preview=PreviewStore(store)
    transcribe_packets([b'x'*6400]*3,p.provider,preview,'live/taobao',threading.Event(),seconds=.4)
    rows=preview.list('live/taobao')
    assert [r['text'] for r in rows]==['一二。','三。']
    assert all(r['final'] for r in rows)
    assert store.list_jobs()==[]


def test_capture_failure_keeps_partial_as_nonfinal(tmp_path):
    import threading
    from app.realtime_worker import transcribe_packets
    from app.realtime_store import PreviewStore
    from app.store import Store
    p,_=provider(['部分'])
    preview=PreviewStore(Store(tmp_path/'state.sqlite'))
    with pytest.raises(IndexError):
        transcribe_packets([b'x'*6400]*2,p.provider,preview,'live/taobao',threading.Event())
    row=preview.list('live/taobao')[0]
    assert row['text']=='部分' and not row['final'] and row['error']=='IndexError'


def test_cancel_releases_remote_session_after_uncertain_packet():
    from app.realtime import StreamRecognizer
    from app.providers import Feishu
    calls=[]
    def handle(request):
        body=json.loads(request.content);calls.append(body)
        if body['config']['action']!=3:raise httpx.ReadTimeout('uncertain')
        return httpx.Response(200,json={'code':0})
    p=Feishu('a','b',httpx.Client(transport=httpx.MockTransport(handle)));p.token='t';p.expires=1e20
    s=StreamRecognizer(p)
    with pytest.raises(httpx.ReadTimeout):s.send(b'x'*6400)
    s.abort()
    assert calls[-1]['config']['action']==3
    assert calls[-1]['config']['sequence_id']==1


def test_rate_limit_has_bounded_backoff():
    from app.realtime import StreamRecognizer, StreamRateLimit
    from app.providers import Feishu
    p=Feishu('a','b',httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'code':10024,'msg':'qps exceeded'}))))
    p.token='t';p.expires=1e20
    with pytest.raises(StreamRateLimit) as e:StreamRecognizer(p).send(b'x'*6400)
    assert e.value.retry_after>=60


@pytest.mark.parametrize('status,code',[(400,10024),(400,99991400),(429,99991400),(200,10024)])
def test_rate_limit_http_and_business_codes_respect_reset(status,code):
    from app.realtime import StreamRecognizer,StreamRateLimit
    from app.providers import Feishu
    p=Feishu('a','b',httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(status,headers={'x-ogw-ratelimit-reset':'120'},json={'code':code}))))
    p.token='t';p.expires=1e20
    with pytest.raises(StreamRateLimit) as e:StreamRecognizer(p).send(b'x'*6400)
    assert e.value.retry_after==120


def test_legacy_rate_limit_without_reset_header():
    from app.realtime import StreamRecognizer,StreamRateLimit
    from app.providers import Feishu
    p=Feishu('a','b',httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(400,json={'code':10024}))))
    p.token='t';p.expires=1e20
    with pytest.raises(StreamRateLimit):StreamRecognizer(p).send(b'x'*6400)
