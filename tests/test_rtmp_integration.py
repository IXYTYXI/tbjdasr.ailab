"""Real RTMP + FFmpeg; remote ASR responses are explicitly simulated."""
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml


@pytest.mark.skipif(not os.getenv('MEDIAMTX_BIN'), reason='Set MEDIAMTX_BIN for real RTMP test')
@pytest.mark.parametrize('video', [False, True])
@pytest.mark.parametrize('multi', [False, True])
def test_real_rtmp_auth_recording_and_audio_extraction(tmp_path, monkeypatch, video, multi):
    from media_entry import configuration
    from app.config import Settings
    from app.worker import Worker
    from app.audio import read_pcm
    import threading
    from unittest.mock import Mock
    import socket
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        api_port = sock.getsockname()[1]
    password = 'integration-test-publish-password'
    monkeypatch.setenv('PUBLISH_PASSWORD', password)
    monkeypatch.setenv('STREAM_PATH', 'live/main')
    monkeypatch.setenv('STREAM_PATHS', 'live/second' if multi else '')
    monkeypatch.setenv('RTMPS_ENABLED', 'false')
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setenv('MEDIA_API_PASSWORD', 'c' * 32)
    monkeypatch.setenv('MEDIA_API_ADDRESS', f'127.0.0.1:{api_port}')
    monkeypatch.setenv('RECORD_SEGMENT_DURATION', '2s')
    hook = shlex.join([sys.executable, str(Path('segment_hook.py').resolve())])
    monkeypatch.setenv('SEGMENT_HOOK_COMMAND', hook if video else 'false')
    cfg = configuration()
    cfg['rtmpAddress'] = f'127.0.0.1:{port}'
    conf = tmp_path / 'mediamtx.yml'
    conf.write_text(yaml.safe_dump(cfg))
    binary = str(Path(os.environ['MEDIAMTX_BIN']).resolve())
    checked = subprocess.run([binary, '--validate-conf', str(conf)], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    with (tmp_path / 'media.log').open('w+') as log:
        proc = subprocess.Popen([binary, str(conf)], stdout=log, stderr=log)
        try:
            for _ in range(50):
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=.1):
                        break
                except OSError:
                    time.sleep(.1)
            source = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-re',
                      '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000']
            if video:
                source += ['-f', 'lavfi', '-i', 'testsrc=size=160x120:rate=10',
                           '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-preset', 'ultrafast', '-g', '20']
            live = multi and video
            duration = 12 if live else 7
            source += ['-t', str(duration), '-c:a', 'aac', '-f', 'flv']
            denied = subprocess.run(source + [f'rtmp://127.0.0.1:{port}/live/main?user=obs&pass=wrong'], capture_output=True, timeout=15)
            assert denied.returncode != 0
            unknown = subprocess.run(source + [f'rtmp://127.0.0.1:{port}/live/unknown?user=obs&pass={password}'], capture_output=True, timeout=15)
            assert unknown.returncode != 0
            from concurrent.futures import ThreadPoolExecutor
            rooms = ['live/main', 'live/second'] if multi else ['live/main']
            worker = Worker(Settings(data=tmp_path, api_key='a'*32, signing_key='b'*32,
                                     public_url='http://example.com', media_api_url=f'http://127.0.0.1:{api_port}',
                                     orphan_grace=0, poll_seconds=.1))
            worker.company = Mock()
            worker.company.poll.return_value = {'text': '模拟识别结果（非真实ASR）', 'raw': {}}
            thread = threading.Thread(target=worker.run) if live else None
            if thread:
                thread.start()
            def send(room):
                command = [s.replace('frequency=440', 'frequency=880') if room == 'live/second' else s for s in source]
                return subprocess.run(command + [f'rtmp://127.0.0.1:{port}/{room}?user=obs&pass={password}'], capture_output=True, timeout=20)
            try:
                with ThreadPoolExecutor(max_workers=len(rooms)) as pool:
                    futures = [pool.submit(send, room) for room in rooms]
                    if live:
                        deadline = time.monotonic() + 10
                        def has_text(room):
                            path = tmp_path / 'transcripts' / (room + '.txt')
                            return path.exists() and '模拟识别结果' in path.read_text()
                        while time.monotonic() < deadline and not all(has_text(room) for room in rooms):
                            time.sleep(.1)
                        assert all(has_text(room) for room in rooms), worker.db.summary()
                        assert all(not future.done() for future in futures), 'Text must appear before publishers stop'
                    for future in futures:
                        sent = future.result()
                        assert sent.returncode == 0, sent.stderr.decode()
                if live:
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        rows = worker.db.list_jobs()
                        if (rows and all(j['state'] == 'succeeded' for j in rows)
                            and all(duration-.2 < sum(j['duration'] for j in rows if j['room'] == room) < duration+.2
                                    for room in rooms)):
                            break
                        time.sleep(.1)
                    else:
                        pytest.fail(f'Tail audio was not transcribed: {worker.db.summary()}')
            finally:
                if thread:
                    worker.stop.set()
                    thread.join(timeout=15)
                    assert not thread.is_alive()
            for _ in range(50):
                worker.reconcile()
                if len(list(tmp_path.rglob('*.ready.json'))) == len(list(tmp_path.rglob('*.mp4'))):
                    break
                time.sleep(.1)
        finally:
            proc.terminate()
            proc.wait(timeout=10)
        log.seek(0)
        contents = log.read()
    # The hook is a child process; server exit doesn't imply the last hook has flushed.
    expected = len(list(tmp_path.rglob('*.mp4')))
    for _ in range(50):
        if len(list(tmp_path.rglob('*.ready.json'))) == expected:
            break
        time.sleep(.1)
    markers = list(tmp_path.rglob('*.ready.json'))
    assert len(markers) == expected
    assert markers, contents
    worker.discover()
    jobs = worker.db.list_jobs()
    assert jobs, worker.db.summary()
    assert {j['room'] for j in jobs} == set(rooms)
    for room in rooms:
        total = sum(j['duration'] for j in jobs if j['room'] == room)
        assert duration-.2 < total < duration+.2, (room, total, contents)
        if live:
            text = (tmp_path / 'transcripts' / (room + '.txt')).read_text()
            assert text.count('模拟识别结果') == sum(j['room'] == room for j in jobs)
    for job in jobs:
        import struct
        pcm = read_pcm(tmp_path / job['path'])
        assert pcm
        # Count positive crossings: different tones prove the streams were not mixed.
        samples = [v[0] for v in struct.iter_unpack('<h', pcm)]
        crossings = sum(a <= 0 < b for a, b in zip(samples, samples[1:]))
        frequency = crossings / (len(samples) / 16000)
        expected_hz = 880 if job['room'] == 'live/second' else 440
        assert abs(frequency - expected_hz) < 15, (job['room'], frequency)
    assert worker.db.summary()['assets'] == {'done': len(markers)}
