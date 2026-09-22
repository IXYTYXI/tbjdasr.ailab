"""Remove closed media only after durable Feishu receipts confirm every ASR job."""
import argparse
from contextlib import ExitStack
import fcntl
import hashlib
import json
import math
import os
import re
from pathlib import Path
import sqlite3
import time
from live_feishu_sync import digest

FIELDS=('id','room','start','duration','provider','state','text')


def safe_path(root, path):
    path=Path(path)
    if not path.is_absolute():path=root/path
    relative=path.relative_to(root)
    if '..' in relative.parts:raise ValueError('Path escapes data directory')
    current=root
    for part in relative.parts:
        current=current/part
        if current.is_symlink():raise ValueError('Symlink rejected')
    if not path.resolve().is_relative_to(root):raise ValueError('Path escapes data directory')
    return path


def cleanup(data, receipts, retention_hours=24, apply=False, now=None):
    if not math.isfinite(retention_hours) or retention_hours<0:raise ValueError('Invalid retention')
    data=Path(data).resolve();receipts=Path(receipts).resolve()
    now=time.time() if now is None else now
    if not receipts.is_dir():raise ValueError('Receipt directory missing')
    report={'apply':apply,'eligible':0,'deleted_files':0,'bytes':0,'assets':[]}
    with ExitStack() as stack:
        lock=stack.enter_context((data/'media-cleanup.lock').open('a'))
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        confirmed={}
        # Use the same per-session locks as the writer. An in-progress receipt is skipped.
        for path in receipts.glob('*.json'):
            handle=stack.enter_context(path.with_suffix('.lock').open('a'))
            try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:continue
            s=json.loads(path.read_text())
            if s.get('status')!='ready' or s.get('pending') or s.get('record_pending') or s.get('test_only'):continue
            if not s.get('document_id') or not s.get('record_id'):continue
            for job_id,value in s.get('segments',{}).items():
                confirmed.setdefault(job_id,[]).append((value,s.get('synced_at',now)))
        db=sqlite3.connect('file:'+str(data/'state.sqlite')+'?mode=rw',uri=True,timeout=30)
        stack.callback(db.close);db.row_factory=sqlite3.Row
        db.execute('BEGIN IMMEDIATE')
        for asset in db.execute("SELECT * FROM assets WHERE state='done'").fetchall():
            item={'id':asset['id'],'status':'skipped'}
            report['assets'].append(item)
            try:
                aid=asset['id']
                if len(aid)!=64 or any(c not in '0123456789abcdef' for c in aid):raise ValueError('Invalid asset ID')
                marker_path=Path(asset['marker'])
                if marker_path.is_relative_to('/data'):marker_path=data/marker_path.relative_to('/data')
                marker=safe_path(data,marker_path)
                relative=marker.relative_to(data/'recordings')
                if hashlib.sha256(str(relative).encode()).hexdigest()!=aid:raise ValueError('Asset marker mismatch')
                if not marker.name.endswith('.ready.json') or not marker.is_file():raise ValueError('No closed recording marker')
                source=safe_path(data,marker.with_name(marker.name[:-len('.ready.json')]+'.mp4'))
                info=json.loads(marker.read_text())
                declared=Path(info['path'])
                if declared.is_relative_to('/data'):declared=data/declared.relative_to('/data')
                if safe_path(data,declared)!=source:raise ValueError('Recording path mismatch')
                rows=db.execute("SELECT * FROM jobs WHERE substr(id,1,65)=? ORDER BY id",(aid+'_',)).fetchall()
                if not rows or any(r['state']!='succeeded' for r in rows):raise ValueError('ASR incomplete')
                files=[source];finished=asset['updated']
                for index,r in enumerate(rows):
                    if r['id']!=f'{aid}_{index:05d}' or r['room']!=info['room']:raise ValueError('Missing or mismatched job')
                    values={k:r[k] for k in FIELDS};expected=digest(values)
                    times=[t for value,t in confirmed.get(r['id'],[]) if value==expected]
                    if not times:raise ValueError('Feishu receipt missing or changed')
                    finished=max(finished,min(times))
                    audio=safe_path(data,Path(r['path']))
                    if audio!=data/'audio'/aid/'parts'/f'{index:05d}.wav':raise ValueError('Audio path mismatch')
                    files.append(audio)
                folder=safe_path(data,data/'audio'/aid/'parts')
                if folder.exists() and set(folder.iterdir())-set(files):raise ValueError('Untracked audio files')
                # Keep small markers and DB rows so discovery cannot queue deleted media again.
                for path in files:
                    if path.exists():
                        if not path.is_file():raise ValueError('Not a regular file')
                        finished=max(finished,path.stat().st_mtime)
                if now-finished<retention_hours*3600:raise ValueError('Retention period')
                existing=[p for p in files if p.exists()]
                size=sum(p.stat().st_size for p in existing)
                if not existing:item['status']='already_cleaned';continue
                item.update(status='eligible',files=[str(p.relative_to(data)) for p in existing],bytes=size)
                report['eligible']+=1;report['bytes']+=size
                if apply:
                    # Flush intent before unlink; interrupted runs safely retry remaining files.
                    with (data/'media-cleanup.jsonl').open('a') as audit:
                        audit.write(json.dumps(dict(item,at=now,event='delete_intent'))+'\n');audit.flush();os.fsync(audit.fileno())
                        for path in existing:
                            path.unlink();report['deleted_files']+=1
                        item['status']='deleted'
                        audit.write(json.dumps(dict(item,at=time.time(),event='deleted'))+'\n');audit.flush();os.fsync(audit.fileno())
            except (ValueError,KeyError,OSError) as exc:
                item['reason']=str(exc)
        db.rollback()
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data',default='data')
    group=p.add_mutually_exclusive_group(required=True)
    group.add_argument('--receipts')
    group.add_argument('--sync-config',help='Use the configured production archive receipts')
    p.add_argument('--retention-hours',type=float,default=24)
    p.add_argument('--apply',action='store_true',help='Delete eligible files; default is preview only')
    a=p.parse_args()
    if a.sync_config:
        config=json.loads(Path(a.sync_config).read_text())
        archive=config.get('archive_id','')
        if not re.fullmatch(r'[A-Za-z0-9_-]+',archive):
            raise ValueError('Production archive_id required')
        a.receipts=Path(a.data)/'feishu-sync/sessions/archives'/archive
    print(json.dumps(cleanup(a.data,a.receipts,a.retention_hours,a.apply),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
