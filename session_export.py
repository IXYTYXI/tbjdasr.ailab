"""Archive an explicitly bounded session to user-owned Feishu Docs and Base."""
import argparse
import fcntl
import hashlib
from html import escape
import json
import math
import time
import os
from pathlib import Path
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from app.documents import render_xml
from export_feishu import FOLDER, cli

FIELDS = {'场次编号': 'text', '场次名称': 'text', '直播间': 'text',
          '开始时间': 'datetime', '结束时间': 'datetime', '转写状态': 'text', '转写文档': 'text'}


def call_base(args):
    result = subprocess.run(['lark-cli', 'base', *args, '--as', 'user', '--format', 'json'],
                            capture_output=True, text=True, timeout=180)
    try:
        data = json.loads(result.stdout)
    except ValueError:
        raise RuntimeError('飞书返回不确定，请检查远端结果后再恢复') from None
    if result.returncode or not data.get('ok') or data.get('identity') != 'user':
        raise RuntimeError('飞书 Base 操作失败，请检查用户权限或 CLI 返回')
    return data['data']


class LarkGateway:
    def validate_table(self, base, table):
        data = call_base(['+field-list', '--base-token', base, '--table-id', table, '--limit', '200'])
        fields = data.get('fields', data.get('items', []))
        found = {f['name']: f['type'] for f in fields}
        if any(found.get(k) != v for k, v in FIELDS.items()):
            raise ValueError('目标表字段缺失或类型不符：' + ', '.join(FIELDS))

    def create_document(self, content):
        return cli(['+create', '--parent-token', FOLDER], content)['document']

    def append_document(self, document_id, content):
        cli(['+update', '--doc', document_id, '--command', 'append'], content)

    def find_record(self, base, table, session_id, room):
        # A new local receipt must not silently duplicate an existing business key.
        query = {'logic': 'and', 'conditions': [['场次编号', '==', session_id],
                                               ['直播间', '==', room]]}
        existing = call_base(['+record-list', '--base-token', base, '--table-id', table,
                              '--filter-json', json.dumps(query, ensure_ascii=False), '--limit', '2'])
        records = existing.get('records', existing.get('items'))
        if records is None:
            raise RuntimeError('无法确认表格是否已有此场次，停止创建')
        return records[0] if records else None

    def create_record(self, base, table, fields):
        if self.find_record(base, table, fields['场次编号'], fields['直播间']):
            raise RuntimeError('表格已有此场次，请核对已有记录与本地回执')
        data = call_base(['+record-upsert', '--base-token', base, '--table-id', table,
                          '--json', json.dumps(fields, ensure_ascii=False)])
        record = data['record']
        record_id = record.get('id') or record.get('record_id')
        if not record_id or data.get('ignored_fields'):
            raise RuntimeError('记录写入结果不完整，请核对远端记录')
        return record_id


class SessionExporter:
    def __init__(self, directory, gateway):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.gateway = gateway

    def export(self, session_id, room, since, until, rows, title, base, table):
        if not session_id or not room or not title or not base or not table:
            raise ValueError('场次、直播间、标题和目标表不能为空')
        if not math.isfinite(since) or not math.isfinite(until) or since >= until:
            raise ValueError('场次时间范围无效')
        if until > time.time():
            raise ValueError('场次时间范围尚未结束，不能导出')
        if not rows or any(r['state'] != 'succeeded' for r in rows):
            raise ValueError('场次有未完成或失败的转写，暂不归档')
        if any(r['room'] != room or not since <= r['start'] < until for r in rows):
            raise ValueError('转写片段不属于所选场次')
        rows = sorted(rows, key=lambda r: (r['start'], r['id']))
        snapshot = [{k: r[k] for k in ('id', 'room', 'start', 'duration', 'provider', 'state', 'text')} for r in rows]
        spec = dict(session_id=session_id, room=room, since=float(since), until=float(until), title=title,
                    base=base, table=table, folder=FOLDER, rows=snapshot)
        digest = hashlib.sha256(json.dumps(spec, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        key = hashlib.sha256(json.dumps([session_id, room, base, table, FOLDER]).encode()).hexdigest()
        path = self.directory / (key + '.json')
        with (self.directory / (key + '.lock')).open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            state = json.loads(path.read_text()) if path.exists() else {'status': 'new', 'digest': digest, 'session_id': session_id, 'room': room}
            if state['digest'] != digest:
                raise ValueError('场次内容或边界已变化，请核对已有归档，不能自动重复建文档')
            if state['status'] == 'done':
                return state
            if state['status'].endswith('_uncertain'):
                raise RuntimeError('上次飞书写入结果不确定，请核对文档/记录与回执：' + str(path))
            self.gateway.validate_table(base, table)
            def save(status, **values):
                state.update(status=status, **values)
                temp = path.with_suffix('.tmp')
                with temp.open('w', encoding='utf-8') as output:
                    json.dump(state, output, ensure_ascii=False, indent=2)
                    output.flush()
                    os.fsync(output.fileno())
                temp.replace(path)
            if state['status'] == 'new':
                if self.gateway.find_record(base, table, session_id, room):
                    raise ValueError('表格已有此场次，请复用原回执或核对远端记录')
                save('create_uncertain')
                header = f'<title>{escape(title)}</title><p>场次编号：{escape(session_id)}；直播间：{escape(room)}。</p><p>本篇为所选时间范围内已入库转写的快照；不代表录音采集完整性验收。</p>'
                doc = self.gateway.create_document(header)
                save('doc_ready', document_id=doc['document_id'],
                     url=doc.get('url') or 'https://guanghe.feishu.cn/docx/' + doc['document_id'], next_index=0)
            if state['status'] == 'doc_ready':
                for index in range(state['next_index'], len(rows)):
                    xml = render_xml(room, [rows[index]])
                    fragment = xml[xml.index('</p>') + 4:]
                    save('append_uncertain', next_index=index)
                    self.gateway.append_document(state['document_id'], fragment)
                    save('doc_ready', next_index=index + 1)
                save('doc_done')
            if state['status'] == 'doc_done':
                zone = ZoneInfo('Asia/Shanghai')
                stamp = lambda t: datetime.fromtimestamp(t, zone).strftime('%Y-%m-%d %H:%M:%S')
                fields = {'场次编号': session_id, '场次名称': title, '直播间': room,
                          '开始时间': stamp(min(r['start'] for r in rows)),
                          '结束时间': stamp(max(r['start'] + r['duration'] for r in rows)),
                          '转写状态': '已同步快照', '转写文档': state['url']}
                save('record_uncertain')
                record_id = self.gateway.create_record(base, table, fields)
                save('done', record_id=record_id, base_token=base, table_id=table)
            return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('url', 'room', 'session-id', 'title', 'base-url'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--table-id')
    parser.add_argument('--since', type=float, required=True)
    parser.add_argument('--until', type=float, required=True)
    parser.add_argument('--receipts', default='generated/feishu-sessions')
    args = parser.parse_args()
    key = os.environ.get('API_KEY')
    if not key:
        raise SystemExit('Set API_KEY in the environment')
    resolved = call_base(['+url-resolve', '--url', args.base_url])
    base = resolved['base_token']
    table = args.table_id or resolved.get('table_id')
    if not table:
        raise SystemExit('Provide --table-id or a Base URL containing the target table')
    rows = []
    with httpx.Client(timeout=30, headers={'Authorization': 'Bearer ' + key}) as client:
        while True:
            response = client.get(args.url.rstrip('/') + '/v1/jobs', params={'room': args.room,
                'since': args.since, 'until': args.until, 'limit': 1000, 'offset': len(rows)})
            response.raise_for_status()
            page = response.json(); rows.extend(page)
            if len(page) < 1000:
                break
    result = SessionExporter(args.receipts, LarkGateway()).export(args.session_id, args.room, args.since,
        args.until, rows, args.title, base, table)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
