import threading
from app.store import Store


def job():
    return dict(id='asset_00000',room='live/taobao',path='audio/asset/parts/00000.wav',start=100.,duration=5.,provider='feishu')


def test_stream_job_claim_and_completion_are_single_owner(tmp_path):
    db=Store(tmp_path/'db')
    assert db.reserve_stream_job(job(),deadline=120)
    assert not db.reserve_stream_job(job(),deadline=120)
    assert not db.due()
    assert db.complete_stream_job(job()['id'],'正式文字',{})
    db.add_job(job())
    assert len(db.list_jobs())==1
    assert db.get(job()['id'])['state']=='succeeded'
    assert not db.complete_stream_job(job()['id'],'重复文字',{})


def test_expired_stream_reservation_falls_back_and_cannot_be_overwritten(tmp_path):
    db=Store(tmp_path/'db')
    assert db.reserve_stream_job(job(),deadline=0)
    db.recover_stream_jobs(now=1)
    assert db.get(job()['id'])['state']=='queued'
    assert not db.complete_stream_job(job()['id'],'迟到实时结果',{})


def test_existing_archive_job_wins_race(tmp_path):
    db=Store(tmp_path/'db');db.add_job(job())
    assert not db.reserve_stream_job(job(),deadline=100)
    assert db.get(job()['id'])['state']=='queued'


def mock_provider(fail_finish=False):
    import httpx,json
    from app.providers import Feishu
    def handle(request):
        body=json.loads(request.content);c=body['config']
        if fail_finish and c['action']==2:raise httpx.ReadTimeout('timeout')
        return httpx.Response(200,json={'code':0,'data':{'stream_id':c['stream_id'],'sequence_id':c['sequence_id'],'recognition_text':'最终文字' if c['action']==2 else '尚未定稿'}})
    p=Feishu('a','b',httpx.Client(transport=httpx.MockTransport(handle)));p.token='t';p.expires=1e20;p.stream_min_interval=0
    return p


def test_live_final_creates_canonical_jobs_and_preserves_pcm_tail(tmp_path):
    from app.recording_live import archive_packets
    from app.realtime_store import PreviewStore
    from app.audio import read_pcm
    db=Store(tmp_path/'db');preview=PreviewStore(db)
    packets=[b'\x01\x00'*3200]*25+[b'\x02\x00'*800]
    archive_packets(packets,mock_provider(),preview,db,tmp_path,'live/taobao','asset',100,5,threading.Event())
    rows=db.list_jobs()
    assert [r['id'] for r in rows]==['asset_00000','asset_00001']
    assert [r['duration'] for r in rows]==[5,.05]
    assert all(r['state']=='succeeded' and r['text']=='最终文字' for r in rows)
    assert b''.join(read_pcm(tmp_path/r['path']) for r in rows)==b''.join(packets)
    # Closing-file extraction inserts exactly the same IDs, without duplicate results.
    for r in rows:db.add_job(r)
    assert len(db.list_jobs())==2


def test_finish_failure_leaves_durable_wav_for_file_asr(tmp_path):
    import httpx,pytest
    from app.recording_live import archive_packets
    from app.realtime_store import PreviewStore
    from app.audio import read_pcm
    db=Store(tmp_path/'db')
    with pytest.raises(httpx.ReadTimeout):
        archive_packets([b'\x01\x00'*3200]*25,mock_provider(True),PreviewStore(db),db,tmp_path,'live/taobao','asset',100,5,threading.Event())
    row=db.get('asset_00000')
    assert row['state']=='queued' and row['text']==''
    assert len(read_pcm(tmp_path/row['path']))==160000


def test_segment_boundaries_are_frozen_per_asset(tmp_path):
    db=Store(tmp_path/'db')
    assert db.asset('old','old.json')['segment_seconds']==45
    assert db.asset('new','new.json',segment_seconds=5)['segment_seconds']==5
    assert db.asset('new','new.json',segment_seconds=45)['segment_seconds']==5


def test_shutdown_mid_chunk_never_claims_a_short_canonical_chunk(tmp_path):
    from app.recording_live import archive_packets
    from app.realtime_store import PreviewStore
    db=Store(tmp_path/'db');stop=threading.Event()
    def packets():
        yield b'x'*6400
        stop.set()
    archive_packets(packets(),mock_provider(),PreviewStore(db),db,tmp_path,'live/taobao','asset',100,5,stop)
    assert db.list_jobs()==[]


def test_growing_mp4_and_closed_file_have_identical_pcm(tmp_path):
    import subprocess,shutil,pytest,time
    from app.recording_live import growing_pcm
    from app.audio import extract_audio,read_pcm
    if not shutil.which('ffmpeg'):pytest.skip('Requires ffmpeg')
    source=tmp_path/'recording.mp4';marker=tmp_path/'recording.ready.json'
    process=subprocess.Popen(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-y','-re',
        '-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','8',
        '-c:a','aac','-movflags','frag_keyframe+empty_moov+default_base_moof',
        '-frag_duration','1000000','-flush_packets','1','-f','mp4',str(source)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    def mark():
        process.wait();marker.write_text('{}')
    thread=threading.Thread(target=mark);thread.start()
    stop=threading.Event()
    try:
        deadline=time.monotonic()+5
        while not source.exists():
            assert time.monotonic()<deadline
            time.sleep(.02)
        result=[];before_end=False
        for packet in growing_pcm(source,marker,stop):
            result.append(packet)
            if sum(map(len,result))>=160000 and process.poll() is None:before_end=True
        assert before_end,'Five seconds of PCM must be available before the recording closes'
        extract_audio(source,tmp_path/'closed.wav')
        assert b''.join(result)==read_pcm(tmp_path/'closed.wav')
    finally:
        stop.set()
        if process.poll() is None:process.terminate();process.wait()
        thread.join()
