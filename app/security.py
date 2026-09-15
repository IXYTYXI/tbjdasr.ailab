import hashlib
import hmac
import time


def sign(key, job_id, expires):
    return hmac.new(key.encode(), f'{job_id}:{expires}'.encode(), hashlib.sha256).hexdigest()


def verify(key, job_id, expires, signature, now=None):
    now = time.time() if now is None else now
    return expires >= now and hmac.compare_digest(sign(key, job_id, expires), signature)
