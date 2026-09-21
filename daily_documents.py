"""Persistent daily document registry; session receipts still own their Base rows."""
import fcntl
from datetime import datetime, timedelta
from html import escape
import json
from pathlib import Path
from duty_schedule import ZONE


def calendar_slots(slot):
    """Split a shift at local midnight without changing its personnel assignment."""
    start = slot['since']
    while start < slot['until']:
        day = datetime.fromtimestamp(start, ZONE)
        midnight = datetime.combine(day.date() + timedelta(days=1), datetime.min.time(), ZONE).timestamp()
        end = min(slot['until'], midnight)
        yield dict(slot, since=start, until=end, date=day.date().isoformat())
        start = end


class DailyDocuments:
    def __init__(self, directory, gateway):
        self.directory = Path(directory) / 'daily'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.gateway = gateway

    def get(self, slot, grouping, base, table):
        from live_feishu_sync import atomic_json, digest
        from export_feishu import FOLDER
        day = datetime.fromtimestamp(slot['since'], ZONE).date().isoformat()
        person = slot['personnel'] if grouping == 'daily_person' else ''
        key = digest([grouping, day, person, base, table, FOLDER])
        path = self.directory / (key + '.json')
        with (self.directory / (key + '.lock')).open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if path.exists():
                state = json.loads(path.read_text())
                if state['status'] != 'ready':
                    raise RuntimeError('每日文档创建结果不确定，请核对回执后恢复')
                return state
            atomic_json(path, {'status': 'create_uncertain', 'date': day})
            title = f'{day} {person + " " if person else ""}直播转写'
            xml = f'<title>{escape(title)}</title><p>按北京时间归档，主播归属依据直播排班表。每小时录制完成后转写并追加；下播不足一小时也会处理。</p>'
            xml += '<p>同一主播的多场直播归入同一章节。交班附近的短音频可能包含前后两位主播；原始录音和时间戳保留供核对。</p>'
            doc = self.gateway.create_document(xml)
            state = dict(doc, status='ready', date=day)
            atomic_json(path, state)
            return state
