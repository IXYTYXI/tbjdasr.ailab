"""Recover completion markers after unclean media shutdown. Media MUST be stopped."""
import fcntl
import json
import os
from pathlib import Path
import subprocess


def main():
    data = Path(os.getenv('DATA_DIR', '/data')).resolve()
    with (data / 'media.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Media receiver is still running. Stop media before recovery.')
        count = 0
        for source in (data / 'recordings').rglob('*.mp4'):
            marker = source.with_suffix('.ready.json')
            if marker.exists():
                continue
            probe = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0',
                                    '-show_entries', 'stream=index', '-of', 'json', str(source)],
                                   capture_output=True, timeout=30)
            if probe.returncode or not json.loads(probe.stdout).get('streams'):
                print('Could not recover audio:', source.name)
                continue
            relative = source.relative_to(data / 'recordings')
            temp = marker.with_suffix('.tmp')
            temp.write_text(json.dumps({'path': str(source), 'room': str(relative.parent), 'recovered': True}))
            temp.replace(marker)
            count += 1
        print('Recovered markers:', count)


if __name__ == '__main__':
    main()
