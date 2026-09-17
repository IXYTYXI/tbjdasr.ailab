"""Export a bounded transcript snapshot using an already-authorized lark-cli user."""
from lark_runtime import command
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

import httpx
from app.documents import render_xml

FOLDER = 'Lwiof1pEglCsKgdpAU3caUUpnfh'


def cli(arguments, content):
    result = subprocess.run(command('docs', *arguments, '--as', 'user', '--content', '-'),
                            input=content, text=True, capture_output=True, timeout=180)
    try:
        data = json.loads(result.stdout)
    except ValueError:
        raise RuntimeError('CLI returned no valid JSON; inspect the document before retrying')
    if result.returncode or not data.get('ok') or data.get('identity') != 'user':
        raise RuntimeError('Feishu document operation failed; check lark-cli user authorization')
    if data.get('data', {}).get('result') in ('partial_success', 'failed'):
        raise RuntimeError('Feishu reported a partial write; inspect the document before retrying')
    return data['data']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', required=True)
    parser.add_argument('--room', default='live/main')
    parser.add_argument('--since', required=True, type=float, help='UTC Unix seconds')
    parser.add_argument('--until', required=True, type=float, help='UTC Unix seconds, exclusive')
    args = parser.parse_args()
    api_key = os.environ.get('API_KEY', '')
    if not api_key:
        raise SystemExit('Set API_KEY through your environment, not a command-line argument.')
    rows = []
    with httpx.Client(timeout=30, headers={'Authorization': 'Bearer ' + api_key}) as client:
        while True:
            response = client.get(args.url.rstrip('/') + '/v1/jobs', params={
                'room': args.room, 'since': args.since, 'until': args.until, 'limit': 1000, 'offset': len(rows)})
            response.raise_for_status()
            page = response.json()
            rows.extend(page)
            if len(page) < 1000:
                break
    if not rows:
        raise SystemExit('No transcript segments in this time range.')
    # A snapshot is immutable: rerunning the same snapshot returns the prior receipt.
    digest = hashlib.sha256(json.dumps({'folder': FOLDER, 'rows': rows}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    receipts = Path('generated/feishu-exports')
    receipts.mkdir(parents=True, exist_ok=True)
    receipt = receipts / f'{digest}.json'
    if receipt.exists():
        saved = json.loads(receipt.read_text())
        if saved.get('status') == 'done':
            print(saved['url'])
            return
        raise SystemExit(f'Previous export is incomplete or uncertain. Inspect {receipt} and the target document before retrying; do not blindly repeat writes.')
    state = {'status': 'creating', 'folder': FOLDER}
    def save():
        temporary = receipt.with_suffix('.tmp')
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(receipt)
    save()
    doc = cli(['+create', '--parent-token', FOLDER], render_xml(args.room, []))['document']
    state.update(document_id=doc['document_id'], url=doc.get('url', 'https://guanghe.feishu.cn/docx/' + doc['document_id']))
    save()
    # One audio segment per append keeps each write small and preserves order.
    for index, row in enumerate(rows):
        state.update(status='appending', next_index=index)
        save()
        fragment = render_xml(args.room, [row])
        fragment = fragment[fragment.index('</p>') + 4:]  # remove title and introductory paragraph
        cli(['+update', '--doc', doc['document_id'], '--command', 'append'], fragment)
    state.update(status='done', segment_count=len(rows))
    save()
    print(state['url'])


if __name__ == '__main__':
    main()
