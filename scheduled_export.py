"""Preview/archive ended duty sessions; requires an authorized lark-cli user."""
from lark_runtime import command
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

import httpx
from duty_schedule import parse_schedule, mapped_slots, ZONE
from export_feishu import FOLDER
from session_export import SessionExporter, LarkGateway, call_base


def sheets(args):
    result = subprocess.run(command('sheets', *args, '--as', 'user', '--format', 'json'),
                            capture_output=True, text=True, timeout=180)
    envelope = json.loads(result.stdout)
    if result.returncode or not envelope.get('ok') or envelope.get('identity') != 'user':
        raise RuntimeError('读取用户排班表失败，请检查 lark-cli 授权')
    return envelope['data']


def read_schedule(config):
    book = sheets(['+workbook-info', '--url', config['schedule_url']])
    sheet = next(s for s in book['sheets'] if s['sheet_id'] == config['schedule_sheet_id'])
    chunks = []
    for start in range(1, sheet['row_count'] + 1, 200):
        end = min(start + 199, sheet['row_count'])
        data = sheets(['+csv-get', '--spreadsheet-token', book['token'], '--sheet-id', sheet['sheet_id'],
                       '--range', f'A{start}:H{end}', '--max-chars', '500000'])
        if data.get('has_more') or data['revision'] != book['revision'] or data['row_count'] != end-start+1:
            raise ValueError('排班读取不完整或读取期间已改变，请重新执行')
        chunks.append(data)
    complete = dict(chunks[0], annotated_csv='\n'.join(c['annotated_csv'].rstrip('\n') for c in chunks))
    return parse_schedule(complete)


def fetch_jobs(client, url, room, since, until):
    # Worker-produced segments are bounded to 45 seconds; allow 60s lookback
    # so the piece spanning the start boundary is not dropped.
    rows = []
    offset = 0
    while True:
        response = client.get(url.rstrip('/') + '/v1/jobs', params={
            'room': room, 'since': since-60, 'until': until, 'limit': 1000, 'offset': offset})
        response.raise_for_status()
        page = response.json()
        rows.extend(r for r in page if r['start'] + r['duration'] > since)
        offset += len(page)
        if len(page) < 1000:
            return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='feishu_sessions.json')
    parser.add_argument('--date', required=True, help='Asia/Shanghai YYYY-MM-DD')
    parser.add_argument('--url', default='https://tbjdasr.ai.lab.yc345.tv')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--receipts', default='generated/feishu-sessions')
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    if config['folder_token'] != FOLDER:
        raise ValueError('配置文件夹与导出目标不一致，请统一后再运行')
    schedule = read_schedule(config)
    selected = mapped_slots(schedule, config['room_groups'], args.date)
    if not args.apply:
        print(json.dumps(selected, ensure_ascii=False, indent=2))
        return
    key = os.environ.get('API_KEY')
    if not key:
        raise SystemExit('Set API_KEY in the environment')
    target = call_base(['+url-resolve', '--url', config['base_url']])
    exporter = SessionExporter(args.receipts, LarkGateway())
    outcomes = []
    with httpx.Client(headers={'Authorization': 'Bearer '+key}, timeout=30) as client:
        for slot in selected:
            if slot['until'] > time.time():
                outcomes.append({'room':slot['room'], 'cell':slot['cell'], 'status':'班次尚未结束'})
                continue
            rows = fetch_jobs(client, args.url, slot['room'], slot['since'], slot['until'])
            if not rows or any(r['state'] != 'succeeded' for r in rows):
                outcomes.append({'room':slot['room'], 'cell':slot['cell'], 'status':'暂无音频或转写尚未全部成功'})
                continue
            spec = dict(slot, source=config['schedule_url'])
            session_id = 'duty-' + hashlib.sha256(json.dumps([slot['room'], slot['since'],slot['until']]).encode()).hexdigest()[:20]
            stamp = datetime.fromtimestamp(slot['since'], ZONE).strftime('%Y-%m-%d %H:%M')
            title = f"{stamp} {slot['group']} {slot['personnel']} 直播转写"
            try:
                outcomes.append(exporter.export(session_id, slot['room'], slot['since'], slot['until'], rows,
                                               title, target['base_token'], target['table_id'], schedule=spec))
            except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
                outcomes.append({'room': slot['room'], 'cell': slot['cell'], 'session_id': session_id,
                                 'status': '归档失败，待核对', 'error': str(error)})
    print(json.dumps(outcomes, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
