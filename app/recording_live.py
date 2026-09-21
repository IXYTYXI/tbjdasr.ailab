"""Recognize growing fMP4 recordings; live and fallback share exact chunk IDs."""
import hashlib
import os
from pathlib import Path
import subprocess
import threading
import time
import wave

import httpx
from .providers import Feishu, ProviderError
from .realtime import StreamRecognizer, StreamRateLimit
from .realtime_worker import pcm_packets


def asset_identity(source, root):
    marker = source.with_suffix('.ready.json')
    asset_id = hashlib.sha256(str(marker.relative_to(root)).encode()).hexdigest()
    seconds, micros = source.stem.split('-')
    return asset_id, marker, int(seconds) + int(micros) / 1000000


def persist_pcm(path, pcm):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.live.tmp')
    with wave.open(str(temporary), 'wb') as out:
        out.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        out.writeframes(pcm)
    with temporary.open('rb') as f:
        os.fsync(f.fileno())
    os.replace(temporary, path)


def archive_packets(packets, provider, preview, db, data, room, asset_id, started,
                    seconds, stop, heartbeat=None):
    """Only finalized text enters jobs. Closed-file fallback uses identical IDs."""
    chunk = bytearray()
    pending_audio = bytearray()
    session = None
    index = 0
    skip = False
    reserved_id = None
    size = int(seconds * 32000)

    def finalize():
        nonlocal session, reserved_id
        if not chunk or skip:
            return
        if pending_audio:
            session.send(bytes(pending_audio))
            pending_audio.clear()
        path = Path(data) / 'audio' / asset_id / 'parts' / f'{index:05d}.wav'
        persist_pcm(path, chunk)
        job_id = f'{asset_id}_{index:05d}'
        # A closed-file worker may already own this task. Never overwrite it.
        owned = db.reserve_stream_job(dict(id=job_id, room=room,
            path=str(path.relative_to(data)), start=started + index * seconds,
            duration=len(chunk) / 32000, provider='feishu'), deadline=time.time() + 30)
        reserved_id = job_id if owned else None
        text = session.finish()
        if owned:
            db.complete_stream_job(job_id, text, {'source': 'stream_recognize',
                'pcm_sha256': hashlib.sha256(chunk).hexdigest(), 'stream_id': session.stream_id})
        preview.put(session.stream_id, room, started + index * seconds, text, True)
        reserved_id = None
        session = None

    try:
        for packet in packets:
            if stop.is_set():
                break
            # pcm_packets yields 200ms, and canonical chunks use whole seconds.
            if not chunk:
                skip = db.get(f'{asset_id}_{index:05d}') is not None
                if not skip:
                    session = StreamRecognizer(provider)
            chunk.extend(packet)
            if len(chunk) > size:
                raise ValueError('PCM packet crossed a canonical boundary')
            if not skip:
                pending_audio.extend(packet)
                if len(pending_audio) >= 32000:
                    text = session.send(bytes(pending_audio))
                    pending_audio.clear()
                    preview.put(session.stream_id, room, started + index * seconds, text, False)
                    if heartbeat:
                        heartbeat('streaming')
            if len(chunk) == size:
                finalize()
                chunk.clear()
                index += 1
        # Stopping midway does not make a short segment canonical: fallback owns it.
        if not stop.is_set():
            finalize()
        elif session is not None:
            session.abort()
    except Exception as exc:
        if reserved_id:
            # Durable WAV exists; let file ASR retry immediately.
            db.update(reserved_id, next_at=0)
            db.recover_stream_jobs()
        if session is not None:
            error = str(exc) if isinstance(exc, ProviderError) else type(exc).__name__
            preview.put(session.stream_id, room, started + index * seconds, session.text, False, error)
            if not isinstance(exc, StreamRateLimit):
                session.abort()
        raise


def growing_pcm(source, marker, stop):
    """Feed only appended bytes; marker means EOF is authoritative, not temporary."""
    process = subprocess.Popen(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
        '-probesize', '32', '-analyzeduration', '0', '-f', 'mp4', '-i', 'pipe:0', '-map', '0:a:0', '-vn', '-ac', '1', '-ar', '16000',
        '-c:a', 'pcm_s16le', '-f', 's16le', 'pipe:1'], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    done = threading.Event()
    failures = []

    def feed():
        try:
            with source.open('rb') as f:
                while not stop.is_set() and not done.is_set():
                    block = f.read(65536)
                    if block:
                        view = memoryview(block)
                        while view and not stop.is_set() and not done.is_set():
                            written = process.stdin.write(view)
                            if not written:
                                raise BrokenPipeError()
                            view = view[written:]
                    elif marker.exists():
                        # Re-read once after observing the close marker.
                        block = f.read()
                        if block:
                            view = memoryview(block)
                            while view:
                                written = process.stdin.write(view)
                                if not written:
                                    raise BrokenPipeError()
                                view = view[written:]
                        break
                    else:
                        done.wait(.1)
        except Exception as exc:
            failures.append(type(exc).__name__)
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass

    thread = threading.Thread(target=feed)
    thread.start()
    try:
        yield from pcm_packets(process.stdout, stop, idle_timeout=15)
        if not stop.is_set():
            process.wait(timeout=5)
            if process.returncode != 0 or failures or not marker.exists():
                raise RuntimeError('Growing recording decode did not finish cleanly')
    finally:
        done.set()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill();process.wait()
        thread.join(timeout=5)
        process.stdout.close()


def capture_recording(worker, source, room, asset):
    cfg = worker.cfg
    asset_id, marker, started = asset_identity(source, cfg.data / 'recordings')
    packets = None
    try:
        worker.state(room, 'connecting')
        packets = growing_pcm(source, marker, worker.stop)
        with httpx.Client(timeout=httpx.Timeout(8, connect=5)) as client:
            provider = Feishu(cfg.app_id, cfg.app_secret, client)
            provider.stream_min_interval = 1.05
            archive_packets(packets, provider, worker.preview, worker.db, cfg.data, room,
                asset_id, started, asset['segment_seconds'], worker.stop,
                lambda status: worker.state(room, status))
        worker.state(room, 'recording_complete')
    except Exception as exc:
        error = str(exc) if isinstance(exc, ProviderError) else type(exc).__name__
        if isinstance(exc, StreamRateLimit):
            worker.rate_limit_until = max(worker.rate_limit_until, time.monotonic() + exc.retry_after)
        worker.state(room, 'error', error)
    finally:
        if packets is not None:
            packets.close()


def run_recording_archive(worker):
    worker.preview.interrupt_open()
    root = worker.cfg.data / 'recordings'
    selected = worker.cfg.asr_rooms or worker.cfg.rooms
    handled = set()
    try:
        while not worker.stop.is_set():
            try:
                for room, thread in list(worker.threads.items()):
                    if not thread.is_alive():
                        del worker.threads[room]
                if worker.cfg.realtime_enabled and time.monotonic() >= worker.rate_limit_until:
                    candidates = sorted(root.rglob('*.mp4'))
                    for source in candidates:
                        room = str(source.parent.relative_to(root))
                        if room not in selected or room in worker.threads or str(source) in handled:
                            continue
                        if time.time() - source.stat().st_mtime > 60:
                            continue
                        asset_id, marker, started = asset_identity(source, root)
                        asset = worker.db.asset(asset_id, str(marker), segment_seconds=worker.cfg.realtime_session_seconds)
                        if asset['state'] in ('done', 'failed') or marker.exists():
                            continue  # Closed files already belong to the fallback extractor.
                        handled.add(str(source))
                        thread = threading.Thread(target=capture_recording, args=(worker, source, room, asset))
                        worker.threads[room] = thread
                        thread.start()
                # Bound in-memory history; completed recordings are skipped by asset state.
                handled = {p for p in handled if Path(p).exists() and time.time() - Path(p).stat().st_mtime < 120}
                worker.preview.expire(time.time() - 86400)
                worker.db.heartbeat('realtime', {'at': time.time(), 'ok': True,
                    'enabled': worker.cfg.realtime_enabled, 'archive': True})
            except Exception as exc:
                worker.db.heartbeat('realtime', {'at': time.time(), 'ok': False, 'error': type(exc).__name__})
            worker.stop.wait(.5)
    finally:
        worker.stop.set()
        for thread in worker.threads.values():
            thread.join()
        worker.db.heartbeat('realtime', {'at': time.time(), 'ok': False, 'status': 'stopped'})
