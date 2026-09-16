import os
import re
from dataclasses import dataclass, field
from pathlib import Path


def stream_paths():
    """Keep the legacy path while explicitly allowing additional rooms."""
    paths = [os.getenv('STREAM_PATH', 'live/main').strip()]
    extra = os.getenv('STREAM_PATHS', '').strip()
    if extra:
        paths.extend(p.strip() for p in extra.split(','))
    if any(not re.fullmatch(r'[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+', p) for p in paths):
        raise ValueError('STREAM_PATH / STREAM_PATHS must contain paths like live/main')
    return tuple(dict.fromkeys(paths))


@dataclass
class Settings:
    rooms: tuple[str, ...] = field(default_factory=stream_paths)
    data: Path = field(default_factory=lambda: Path(os.getenv('DATA_DIR', '/data')).resolve())
    provider: str = field(default_factory=lambda: os.getenv('ASR_PROVIDER', 'company'))
    public_url: str = field(default_factory=lambda: os.getenv('PUBLIC_BASE_URL', '').rstrip('/'))
    api_key: str = field(default_factory=lambda: os.getenv('API_KEY', ''))
    signing_key: str = field(default_factory=lambda: os.getenv('AUDIO_SIGNING_KEY', ''))
    company_url: str = field(default_factory=lambda: os.getenv('COMPANY_ASR_URL', 'http://101.126.77.69:8080'))
    company_host: str = field(default_factory=lambda: os.getenv('COMPANY_ASR_HOST', 'qwen3-dual-asr.ai'))
    company_uid: str = field(default_factory=lambda: os.getenv('COMPANY_ASR_UID', 'live-audio'))
    hotwords: str = field(default_factory=lambda: os.getenv('ASR_HOTWORDS', ''))
    app_id: str = field(default_factory=lambda: os.getenv('FEISHU_APP_ID', ''))
    app_secret: str = field(default_factory=lambda: os.getenv('FEISHU_APP_SECRET', ''))
    media_api_url: str = field(default_factory=lambda: os.getenv('MEDIA_API_URL', 'http://media:9997'))
    media_api_password: str = field(default_factory=lambda: os.getenv('MEDIA_API_PASSWORD', ''))
    orphan_grace: int = 10
    url_ttl: int = 7 * 86400
    poll_seconds: int = 5
    max_errors: int = 8
    task_timeout: int = 86400

    def validate(self):
        if self.provider not in ('company', 'feishu'):
            raise ValueError('ASR_PROVIDER must be company or feishu')
        if len(self.api_key) < 24 or len(self.signing_key) < 24:
            raise ValueError('Run init_config.py to generate API_KEY and AUDIO_SIGNING_KEY')
        if self.provider == 'company' and not self.public_url.startswith(('http://', 'https://')):
            raise ValueError('Company ASR requires PUBLIC_BASE_URL reachable from its server')
        self.data.mkdir(parents=True, exist_ok=True)
