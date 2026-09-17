import base64,wave
import httpx
from app.providers import Feishu


def test_long_pcm_is_split_without_missing_or_duplicate_samples(tmp_path):
    path=tmp_path/'audio.wav';pcm=b'\x01\x02'*(16000*33+17)
    with wave.open(str(path),'wb') as w:
        w.setparams((1,2,16000,0,'NONE','not compressed'));w.writeframes(pcm)
    sent=[];ids=[]
    def handler(r):
        import json
        body=json.loads(r.content)
        if 'auth/' in str(r.url):return httpx.Response(200,json={'code':0,'tenant_access_token':'t','expire':7200})
        sent.append(base64.b64decode(body['speech']['speech']));ids.append(body['config']['file_id'])
        return httpx.Response(200,json={'code':0,'data':{'recognition_text':str(len(sent))}})
    p=Feishu('a','s',httpx.Client(transport=httpx.MockTransport(handler)))
    result=p.transcribe('job',path)
    assert len(sent)==3 and max(map(len,sent))<=15*32000
    assert b''.join(sent)==pcm and len(set(ids))==3
    assert result['text']=='1\n2\n3'


def test_room_filter_does_not_schedule_duplicate_stream(tmp_path):
    from app.store import Store
    db=Store(tmp_path/'state.db')
    for room in ['live/main','live/taobao']:
        db.add_job(dict(id=room,room=room,path='a.wav',start=1,duration=3,provider='feishu'))
    assert [r['room'] for r in db.due(rooms=('live/taobao',))]==['live/taobao']
    assert len(db.list_jobs())==2


def test_subrequest_failure_never_returns_partial_success(tmp_path):
    import pytest,json
    from app.providers import ProviderError
    path=tmp_path/'audio.wav'
    with wave.open(str(path),'wb') as w:w.setparams((1,2,16000,0,'NONE','not compressed'));w.writeframes(b'\0\0'*16000*20)
    calls=[]
    def handler(r):
        if 'auth/' in str(r.url):return httpx.Response(200,json={'code':0,'tenant_access_token':'t','expire':7200})
        calls.append(r)
        return httpx.Response(504) if len(calls)==2 else httpx.Response(200,json={'code':0,'data':{'recognition_text':'部分文本'}})
    p=Feishu('a','s',httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ProviderError,match='504'):p.transcribe('job',path)


def test_unknown_asr_room_rejected(tmp_path):
    import pytest
    from app.config import Settings
    cfg=Settings(data=tmp_path,asr_rooms=('live/missing',))
    with pytest.raises(ValueError,match='subset'):cfg.validate()
