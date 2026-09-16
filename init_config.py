"""Run inside the service image, so the server only needs Docker."""
import argparse
import os
from pathlib import Path
import secrets

parser = argparse.ArgumentParser()
parser.add_argument('--server', default='118.196.114.88')
parser.add_argument('--directory', default='.')
args = parser.parse_args()
folder = Path(args.directory)
folder.mkdir(parents=True, exist_ok=True)
target = folder / '.env'
if target.exists():
    raise SystemExit('.env already exists; edit it instead of replacing credentials.')
if any(c in args.server for c in '\r\n/$'):
    raise SystemExit('Use a server hostname or IP, without scheme or path.')
content = f'''PUBLIC_BASE_URL=http://{args.server}:8088
DATA_DIR=/data
ASR_PROVIDER=company
RECORD_SEGMENT_DURATION=30s
API_KEY={secrets.token_urlsafe(32)}
AUDIO_SIGNING_KEY={secrets.token_urlsafe(32)}
PUBLISH_PASSWORD={secrets.token_urlsafe(32)}
MEDIA_API_PASSWORD={secrets.token_urlsafe(32)}
STREAM_PATH=live/main
STREAM_PATHS=live/taobao,live/jingdong
COMPANY_ASR_URL=http://101.126.77.69:8080
COMPANY_ASR_HOST=qwen3-dual-asr.ai
COMPANY_ASR_UID=live-audio
ASR_HOTWORDS=
FEISHU_APP_ID=
FEISHU_APP_SECRET=
RTMPS_ENABLED=false
'''
fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as f:
    f.write(content)
print('Created .env with random credentials. Keep it private. Configure domain/TLS before public production use.')
