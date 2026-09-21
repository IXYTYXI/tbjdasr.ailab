"""Generate MediaMTX config without interpolating secrets in shell commands."""
import os
import sys
from pathlib import Path
import yaml
from app.config import stream_paths


def configuration():
    password = os.environ.get('PUBLISH_PASSWORD', '')
    if len(password) < 24:
        raise ValueError('PUBLISH_PASSWORD must be generated with init_config.py')
    rooms = stream_paths()
    cfg = {
        'logLevel': 'info',
        'authMethod': 'internal',
        'authInternalUsers': [{'user': 'obs', 'pass': password, 'permissions': [{'action': 'publish', 'path': room} for room in rooms]}],
        'rtmp': True, 'rtmpAddress': ':1935',
        'rtsp': False, 'hls': False, 'webrtc': False, 'srt': False,
        'moq': False, 'api': False, 'playback': False,
        'pathDefaults': {
            'record': True,
            'recordPath': str(Path(os.getenv('DATA_DIR', '/data')) / 'recordings' / '%path' / '%s-%f'),
            'recordFormat': 'fmp4', 'recordPartDuration': '1s',
            'recordSegmentDuration': os.getenv('RECORD_SEGMENT_DURATION', '30s'),
            'recordDeleteAfter': '0s', 'overridePublisher': False,
            'runOnRecordSegmentComplete': os.getenv('SEGMENT_HOOK_COMMAND', 'python /app/segment_hook.py'),
        },
        'paths': {room: {} for room in rooms},
    }
    api_password = os.environ.get('MEDIA_API_PASSWORD', '')
    if api_password:
        if len(api_password) < 24:
            raise ValueError('MEDIA_API_PASSWORD must have at least 24 characters')
        cfg.update(api=True, apiAddress=os.getenv('MEDIA_API_ADDRESS', ':9997'))
        cfg['authInternalUsers'].append({'user': 'worker', 'pass': api_password, 'permissions': [{'action': 'api'}]})
    if os.getenv('REALTIME_ASR_ENABLED', 'false').lower() == 'true':
        if not api_password:
            raise ValueError('Realtime requires MEDIA_API_PASSWORD')
        selected = tuple(p.strip() for p in os.getenv('ASR_STREAM_PATHS', '').split(',') if p.strip()) or rooms
        if not set(selected) <= set(rooms):
            raise ValueError('ASR_STREAM_PATHS must be configured media paths')
        cfg.update(rtsp=True, rtspAddress=':8554', rtspTransports=['tcp'])
        cfg['authInternalUsers'][-1]['permissions'].extend({'action': 'read', 'path': room} for room in selected)
    if os.getenv('RTMPS_ENABLED', 'false').lower() == 'true':
        cfg.update(rtmpEncryption='strict', rtmpsAddress=':1936',
                   rtmpServerKey='/certs/server.key', rtmpServerCert='/certs/server.crt')
    return cfg


if __name__ == '__main__':
    import fcntl
    data = Path(os.getenv('DATA_DIR', '/data'))
    data.mkdir(parents=True, exist_ok=True)
    lock = (data / 'media.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.set_inheritable(lock.fileno(), True)
    path = Path('/tmp/live-mediamtx.yml')
    path.write_text(yaml.safe_dump(configuration(), sort_keys=False), encoding='utf-8')
    path.chmod(0o600)
    os.execv('/usr/local/bin/mediamtx', ['mediamtx', str(path)])
