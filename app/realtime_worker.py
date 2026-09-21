"""Optional live preview. Recording + file ASR remains the authoritative archive."""
import fcntl
import logging
import os
import select
import signal
import subprocess
import threading
import time
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from .config import Settings
from .providers import Feishu, ProviderError
from .realtime import StreamRecognizer, StreamRateLimit
from .realtime_store import PreviewStore
from .store import Store

log = logging.getLogger(__name__)


def pcm_packets(pipe, stop, idle_timeout=10):
    """Non-blocking read, bounded memory, preserve the last partial PCM packet."""
    pending = bytearray()
    last = time.monotonic()
    while not stop.is_set():
        if not select.select([pipe], [], [], .2)[0]:
            if time.monotonic() - last > idle_timeout:
                raise TimeoutError('Audio stalled')
            continue
        data = os.read(pipe.fileno(), 6400 - len(pending))
        if not data:
            if len(pending) % 2:
                raise ValueError('Truncated PCM sample')
            if pending:
                yield bytes(pending)
            return
        last = time.monotonic()
        pending.extend(data)
        if len(pending) == 6400:
            yield bytes(pending)
            pending.clear()


def audio_command(cfg, room):
    base = urlsplit(cfg.realtime_rtsp_url)
    if base.scheme != 'rtsp' or not base.hostname or base.username or base.query or base.fragment:
        raise ValueError('MEDIA_RTSP_URL must be an internal rtsp://host:port address')
    url = urlunsplit(('rtsp', 'worker:' + quote(cfg.media_api_password, safe='') + '@' + base.netloc,
                      '/' + room, '', ''))
    return ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-rtsp_transport', 'tcp',
            '-timeout', '10000000', '-i', url, '-map', '0:a:0', '-vn', '-ac', '1',
            '-ar', '16000', '-c:a', 'pcm_s16le', '-f', 's16le', 'pipe:1']


def transcribe_packets(packets, provider, preview, room, stop, seconds=15, heartbeat=None):
    session = None
    samples = 0
    start = 0
    capture_started = None
    received_bytes = 0
    try:
        for packet in packets:
            if stop.is_set():
                break
            if capture_started is None:
                capture_started = time.monotonic()
            received_bytes += len(packet)
            # Avoid showing an ever-growing queue as "live"; archive fills all gaps.
            if time.monotonic() - capture_started - received_bytes / 32000 > 5:
                raise TimeoutError('Realtime fell behind')
            if session is None:
                session = StreamRecognizer(provider)
                start = time.time()
                samples = 0
            text = session.send(packet)
            samples += len(packet)
            preview.put(session.stream_id, room, start, text, False)
            if heartbeat:
                heartbeat('streaming')
            if samples >= seconds * 32000:
                text = session.finish()
                preview.put(session.stream_id, room, start, text, True)
                session = None
        if session is not None and not session.closed:
            text = session.finish()
            preview.put(session.stream_id, room, start, text, True)
    except Exception as exc:
        if session is not None:
            message = str(exc) if isinstance(exc, ProviderError) else type(exc).__name__
            preview.put(session.stream_id, room, start, session.text, False, message)
            if not isinstance(exc, StreamRateLimit):
                session.abort()
        raise


class RealtimeWorker:
    def __init__(self, cfg):
        cfg.validate()
        self.cfg = cfg
        self.db = Store(cfg.data / 'state.sqlite')
        self.preview = PreviewStore(self.db)
        self.stop = threading.Event()
        self.threads = {}
        self.rate_limit_until = 0

    def state(self, room, status, error=''):
        self.db.heartbeat('realtime:' + room, {'at': time.time(), 'status': status, 'error': error})

    def online_rooms(self):
        with httpx.Client(timeout=5) as client:
            response = client.get(self.cfg.media_api_url.rstrip('/') + '/v3/paths/list',
                                  params={'itemsPerPage': 1000}, auth=('worker', self.cfg.media_api_password))
            response.raise_for_status()
            data = response.json()
        if data.get('pageCount', 1) > 1:
            raise ValueError('Media paths pagination exceeded')
        return {item['name'] for item in data['items'] if item.get('online', item.get('ready', False))}

    def capture(self, room):
        process = None
        try:
            self.state(room, 'connecting')
            # Never log argv or stderr: the local reader URL carries credentials.
            process = subprocess.Popen(audio_command(self.cfg, room), stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, bufsize=0)
            with httpx.Client(timeout=httpx.Timeout(8, connect=5)) as client:
                provider = Feishu(self.cfg.app_id, self.cfg.app_secret, client)
                transcribe_packets(pcm_packets(process.stdout, self.stop), provider, self.preview,
                                   room, self.stop, self.cfg.realtime_session_seconds,
                                   lambda state: self.state(room, state))
            if process.poll() not in (None, 0) and not self.stop.is_set():
                raise RuntimeError('Live audio reader exited')
            self.state(room, 'disconnected')
        except Exception as exc:
            message = str(exc) if isinstance(exc, ProviderError) else type(exc).__name__
            if isinstance(exc, StreamRateLimit):
                self.rate_limit_until = max(self.rate_limit_until, time.monotonic() + exc.retry_after)
            self.state(room, 'error', message)
            log.warning('Realtime %s: %s', room, message)
        finally:
            if process:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill();process.wait()
                process.stdout.close()

    def run(self):
        self.preview.interrupt_open()
        rooms = self.cfg.asr_rooms or self.cfg.rooms
        retry_at = {}
        try:
            while not self.stop.is_set():
                try:
                    online = self.online_rooms() if self.cfg.realtime_enabled else set()
                    for room in rooms:
                        thread = self.threads.get(room)
                        if thread and thread.is_alive():
                            continue
                        if thread:
                            del self.threads[room]
                            retry_at[room] = time.monotonic() + 10
                        if room not in online:
                            self.state(room, 'offline' if self.cfg.realtime_enabled else 'disabled')
                        elif time.monotonic() >= max(retry_at.get(room, 0), self.rate_limit_until):
                            thread = threading.Thread(target=self.capture, args=(room,))
                            self.threads[room] = thread
                            thread.start()
                    self.preview.expire(time.time() - 86400)
                    self.db.heartbeat('realtime', {'at': time.time(), 'ok': True, 'enabled': self.cfg.realtime_enabled})
                except Exception as exc:
                    self.db.heartbeat('realtime', {'at': time.time(), 'ok': False, 'error': type(exc).__name__})
                self.stop.wait(2)
        finally:
            self.stop.set()
            for thread in self.threads.values():
                thread.join()
            self.db.heartbeat('realtime', {'at': time.time(), 'ok': False, 'status': 'stopped'})


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    worker = RealtimeWorker(Settings())
    with (worker.cfg.data / 'realtime.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: worker.stop.set())
        worker.run()
