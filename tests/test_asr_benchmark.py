
def test_summary_distinguishes_coverage_and_latency():
    from benchmark_asr import summarize
    events=[{'mode':'short_file','kind':'result','ok':True,'offset':0,'duration':30,'text':'你好','at':140},
            {'mode':'short_file','kind':'result','ok':False,'offset':30,'duration':30,'text':'','at':180}]
    s=summarize(events,100,60)['short_file']
    assert s['first_text_seconds']==40
    assert s['successful_audio_seconds']==30
    assert s['coverage_ratio']==.5
    assert s['failed_segments']==1


def test_empty_text_does_not_claim_first_text_and_modes_are_separate():
    from benchmark_asr import summarize
    events=[{'mode':'realtime','kind':'result','ok':True,'offset':0,'duration':5,'text':'','at':110},
            {'mode':'batch_file','kind':'result','ok':True,'offset':0,'duration':30,'text':'文字','at':150}]
    s=summarize(events,100,30)
    assert s['realtime']['first_text_seconds'] is None
    assert s['batch_file']['first_text_seconds']==50
    assert s['short_file']['successful_audio_seconds']==0


def test_capture_uses_identical_audio_for_short_and_batch_modes(tmp_path,monkeypatch):
    import wave,time
    import benchmark_asr as module
    source=tmp_path/'recordings/live/test'/f'{int(time.time())+1}-000000.mp4'
    source.parent.mkdir(parents=True);source.write_bytes(b'fixture')
    pcm=b'\x01\x00'*16000
    monkeypatch.setattr(module,'growing_pcm',lambda *args:(block for block in [pcm[:6400]]*10))
    bench=module.Benchmark(tmp_path/'output',seconds=2,wait_seconds=1)
    bench.capture(tmp_path/'recordings','live/test')
    assert bench.captured.is_set() and abs(bench.duration-2)<1e-6
    with wave.open(str(bench.root/'capture.wav'),'rb') as f:whole=f.readframes(f.getnframes())
    with wave.open(str(bench.files[0][0]),'rb') as f:part=f.readframes(f.getnframes())
    assert whole==part and len(whole)==64000
    assert bench.file_queue.get()==bench.files[0]
    assert bench.stream_queue.get()+bench.stream_queue.get()==whole


def test_attach_active_older_recording_skips_pretest_audio(tmp_path,monkeypatch):
    import benchmark_asr as module
    source=tmp_path/'recordings/live/test/100-000000.mp4'
    source.parent.mkdir(parents=True);source.write_bytes(b'fixture')
    old=b'\x01\x00'*16000;live=b'\x02\x00'*16000
    monkeypatch.setattr(module,'growing_pcm',lambda *args:(block for block in [old,live]))
    bench=module.Benchmark(tmp_path/'out',seconds=1,wait_seconds=1);bench.armed=101
    bench.capture(tmp_path/'recordings','live/test')
    assert bench.duration==1
    assert bench.stream_queue.get()==live


def test_recording_packets_continues_across_hourly_file_rotation(tmp_path,monkeypatch):
    import threading
    import benchmark_asr as module
    a=tmp_path/'100-000000.mp4';b=tmp_path/'200-000000.mp4'
    a.touch();b.touch()
    monkeypatch.setattr(module,'growing_pcm',lambda source,*args:(x for x in [source.name.encode()]))
    stream=module.recording_packets(a,threading.Event())
    assert next(stream)==a.name.encode()
    assert next(stream)==b.name.encode()
    stream.close()
