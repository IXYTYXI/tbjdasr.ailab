"""Ordered Feishu HTTP streaming session; no replay after an uncertain response."""
import base64
import re
import uuid
from .providers import ProviderError, checked


class StreamRateLimit(ProviderError):
    def __init__(self, retry_after=60):
        super().__init__('Feishu streaming rate limited', True)
        self.retry_after = max(60, min(3600, retry_after))


class StreamRecognizer:
    def __init__(self, provider, stream_id=None):
        self.provider = provider
        self.stream_id = stream_id or uuid.uuid4().hex[:16]
        if not re.fullmatch(r'[A-Za-z0-9_]{16}', self.stream_id):
            raise ValueError('stream_id must contain 16 letters/digits/underscores')
        self.sequence = 0
        self.closed = False
        self.text = ''
        self.result_sequence = -1

    def _request(self, pcm, action):
        if self.closed:
            raise ValueError('Stream is closed; start a new session')
        p = self.provider
        payload = {'speech': {'speech': base64.b64encode(pcm).decode()},
                   'config': {'stream_id': self.stream_id, 'sequence_id': self.sequence,
                              'action': action, 'format': 'pcm', 'engine_type': '16k_auto'}}
        try:
            response = p.client.post(p.BASE + '/speech_to_text/v1/speech/stream_recognize',
                                     headers={'Authorization': 'Bearer ' + p.tenant_token()}, json=payload)
            if response.status_code == 401:
                p.token = ''
            try:
                body_code = response.json().get('code')
            except (ValueError, AttributeError):
                body_code = None
            if response.status_code == 429 or body_code in (10024, 99991400):
                try:
                    retry_after = int(response.headers.get('x-ogw-ratelimit-reset', '60'))
                except ValueError:
                    retry_after = 60
                raise StreamRateLimit(retry_after)
            data = checked(response)
            if data.get('code') != 0:
                if data.get('code') in (99991663, 99991664, 99991668):
                    p.token = ''
                raise ProviderError('Feishu stream code ' + str(data.get('code')))
            result = data.get('data', {})
            # Observed API prefixes the tenant ID and numbers recognition revisions,
            # not incoming audio packets. Revisions may repeat while the model works.
            returned_id = result.get('stream_id', '')
            revision = result.get('sequence_id')
            if not isinstance(returned_id, str) or not (returned_id == self.stream_id or returned_id.endswith('_' + self.stream_id)):
                raise ProviderError('Feishu stream response session mismatch')
            if not isinstance(revision, int) or revision < self.result_sequence:
                raise ProviderError('Feishu stream response revision regressed')
            self.result_sequence = revision
            text = result.get('recognition_text')
            if not isinstance(text, str):
                raise ProviderError('Feishu stream response missing recognition_text')
            self.sequence += 1
            self.text = text
            return text
        except Exception:
            self.closed = True
            raise
        finally:
            if action in (2, 3):
                self.closed = True

    def send(self, pcm):
        if not pcm or len(pcm) % 2 or len(pcm) > 6400:
            raise ValueError('Expect 1-200ms mono PCM16 at 16kHz')
        return self._request(pcm, 1 if self.sequence == 0 else 0)

    def finish(self):
        if not self.sequence:
            raise ValueError('Cannot finish an empty stream')
        return self._request(b'', 2)

    def abort(self):
        """Best-effort release after an uncertain request; never replay audio."""
        self.closed = True
        p = self.provider
        try:
            p.client.post(p.BASE + '/speech_to_text/v1/speech/stream_recognize',
                          headers={'Authorization': 'Bearer ' + p.tenant_token()},
                          json={'speech': {'speech': ''}, 'config': {
                              'stream_id': self.stream_id, 'sequence_id': self.sequence + 1,
                              'action': 3, 'format': 'pcm', 'engine_type': '16k_auto'}})
        except Exception:
            pass
