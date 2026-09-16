from datetime import datetime, timezone
from html import escape
import hashlib
import re


def text_chunks(room, jobs):
    yield f'{room} 直播逐字稿\n时间为 UTC；按片段开始时间排列，排队和失败片段保留状态。\n\n'
    for row in jobs:
        stamp = datetime.fromtimestamp(row['start'], timezone.utc).isoformat()
        yield f'[{stamp} · {row["duration"]:.2f} 秒 · {row["provider"]}]\n'
        if row['state'] == 'succeeded':
            yield (row['text'] or '（未识别到文字）') + '\n\n'
        else:
            yield f'（转写状态：{row["state"]}）\n\n'


def save_transcript(data, room, jobs):
    """Atomically rebuild a derived file; SQLite remains the source of truth."""
    if not re.fullmatch(r'[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+', room):
        raise ValueError('Invalid transcript room')
    target = data / 'transcripts' / (room + '.txt')
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.tmp')
    digest = hashlib.sha256()
    try:
        with temporary.open('wb') as output:
            for chunk in text_chunks(room, jobs):
                content = chunk.encode('utf-8')
                digest.update(content)
                output.write(content)
        if target.exists():
            with target.open('rb') as previous:
                if hashlib.file_digest(previous, 'sha256').digest() == digest.digest():
                    return
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def render_xml(room, jobs):
    parts = [f'<title>{escape(room)} 直播逐字稿</title>',
             '<p>按音频片段开始时间（UTC）排列；时间是片段级定位，非逐词时间戳。未完成或失败片段会明确标记。</p>']
    for row in jobs:
        stamp = datetime.fromtimestamp(row['start'], timezone.utc).isoformat()
        parts.append(f'<p><b>{stamp} · {row["duration"]:.2f} 秒 · {escape(row["provider"])}</b></p>')
        if row['state'] == 'succeeded':
            content = escape(row['text']).replace('\n', '<br/>') or '（未识别到文字）'
        else:
            content = f'（转写状态：{escape(row["state"])}）'
        # Bound each XML paragraph so long ASR results remain manageable.
        parts.append(f'<p>{content}</p>')
    return ''.join(parts)
