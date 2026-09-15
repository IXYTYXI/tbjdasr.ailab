from datetime import datetime, timezone
from html import escape


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
