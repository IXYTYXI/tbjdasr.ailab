import base64
import hashlib
import time

import httpx
from .audio import read_pcm


class ProviderError(Exception):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def checked(response):
    if response.status_code >= 400:
        raise ProviderError(f'ASR HTTP {response.status_code}', response.status_code in (408, 409, 429) or response.status_code >= 500)
    try:
        return response.json()
    except ValueError:
        raise ProviderError('ASR returned invalid JSON', True)


class Company:
    def __init__(self, url, host, uid, hotwords='', client=None):
        self.url = url.rstrip('/')
        self.host, self.uid, self.hotwords = host, uid, hotwords
        self.client = client or httpx.Client(timeout=30, follow_redirects=False)

    def post(self, route, task_id, body=None):
        headers = {'X-Api-Request-Id': task_id, 'Content-Type': 'application/json'}
        if self.host:
            headers['Host'] = self.host
        response = self.client.post(f'{self.url}/asr/v1/qwen3/{route}', headers=headers, json=body)
        data = checked(response)
        return response, data

    def submit(self, task_id, audio_url, payload=None):
        request = {'model_name': 'qwen3'}
        if self.hotwords:
            request['hotwords'] = self.hotwords
        _, data = self.post('submit', task_id, payload or {'user': {'uid': self.uid}, 'audio': {'url': audio_url}, 'request': request})
        if str(data.get('code')) != '20000000':
            raise ProviderError('Company submit code ' + str(data.get('code')), str(data.get('code', '')).startswith('5'))

    def poll(self, task_id):
        response, data = self.post('query', task_id)
        code = response.headers.get('X-Api-Status-Code') or str(data.get('code', ''))
        if code in ('20000001', '20000002'):
            return None
        if code != '20000000':
            raise ProviderError('Company query code ' + code)
        _, data = self.post('result', task_id)
        result = data.get('result', {})
        if not isinstance(result, dict) or not isinstance(result.get('text'), str):
            raise ProviderError('Company result missing result.text')
        return {'text': result['text'], 'raw': data}


class Feishu:
    BASE = 'https://open.feishu.cn/open-apis'

    def __init__(self, app_id, app_secret, client=None):
        self.app_id, self.app_secret = app_id, app_secret
        self.client = client or httpx.Client(timeout=60, follow_redirects=False)
        self.token, self.expires = '', 0

    def tenant_token(self):
        if self.token and time.time() < self.expires:
            return self.token
        if not self.app_id or not self.app_secret:
            raise ProviderError('FEISHU_APP_ID / FEISHU_APP_SECRET not configured')
        data = checked(self.client.post(self.BASE + '/auth/v3/tenant_access_token/internal',
                                       json={'app_id': self.app_id, 'app_secret': self.app_secret}))
        if data.get('code') != 0 or not data.get('tenant_access_token'):
            raise ProviderError('Feishu token error code ' + str(data.get('code')))
        self.token = data['tenant_access_token']
        self.expires = time.time() + max(1, int(data.get('expire', 7200)) - 120)
        return self.token

    def transcribe(self, job_id, path):
        payload = {'speech': {'speech': base64.b64encode(read_pcm(path)).decode()},
                   'config': {'file_id': hashlib.sha256(job_id.encode()).hexdigest()[:16],
                              'format': 'pcm', 'engine_type': '16k_auto'}}
        response = self.client.post(self.BASE + '/speech_to_text/v1/speech/file_recognize',
                                    headers={'Authorization': 'Bearer ' + self.tenant_token()}, json=payload)
        if response.status_code == 401:
            self.token = ''
            raise ProviderError('Feishu token expired', True)
        data = checked(response)
        code = data.get('code')
        if code != 0:
            if code in (99991663, 99991664, 99991668):
                self.token = ''
            raise ProviderError('Feishu ASR code ' + str(code), code in (1040102, 99991400, 99991663, 99991664, 99991668))
        text = data.get('data', {}).get('recognition_text')
        if not isinstance(text, str):
            raise ProviderError('Feishu response missing recognition_text')
        return {'text': text, 'raw': data}
