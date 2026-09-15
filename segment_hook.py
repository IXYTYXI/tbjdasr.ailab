"""MediaMTX segment-complete hook: persist notification without requiring API uptime."""
import json
import os
from pathlib import Path


def mark():
    path = Path(os.environ['MTX_SEGMENT_PATH']).resolve()
    target = path.with_suffix('.ready.json')
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps({'path': str(path), 'room': os.environ['MTX_PATH'],
                                      'duration': os.environ.get('MTX_SEGMENT_DURATION') }), encoding='utf-8')
    with temporary.open('rb') as f:
        os.fsync(f.fileno())
    temporary.replace(target)


if __name__ == '__main__':
    mark()
