"""Continuously synchronize scheduled transcripts using a user-authorized CLI."""
import argparse
from datetime import datetime
import fcntl
import json
import logging
import signal
import sqlite3
from pathlib import Path
import threading
import time
from duty_schedule import mapped_slots,ZONE
from scheduled_export import read_schedule
from session_export import call_base
from live_feishu_sync import LiveSessionSync,LiveGateway,atomic_json
from export_feishu import FOLDER

log=logging.getLogger(__name__)


def read_rows(database,room,since,until,by_start=False):
    with sqlite3.connect('file:'+str(Path(database).resolve())+'?mode=ro',uri=True,timeout=30) as db:
        db.row_factory=sqlite3.Row
        condition='start>=?' if by_start else 'start+duration>?'
        return [dict(row) for row in db.execute('SELECT id,room,start,duration,provider,state,text '
            'FROM jobs WHERE room=? AND start<? AND '+condition+' ORDER BY start,id',(room,until,since))]


def run_once(config,database,directory,schedule,target,gateway,now=None):
    now=time.time() if now is None else now
    grouping=config.get('document_grouping','session')
    sync=LiveSessionSync(directory,gateway,grouping=grouping,chapter_layout=config.get('chapter_layout','timeline'))
    days=sorted({slot['date'] for slot in schedule if slot['since']<=now})
    def key(slot):return (slot['room'],slot['since'],slot['until'])
    selected={key(slot):slot for day in days for slot in mapped_slots(schedule,config['room_groups'],day)}
    if grouping != 'session':
        from daily_documents import calendar_slots
        selected={key(part):part for slot in selected.values() for part in calendar_slots(slot)}
    legacy_keys=set()
    # Keep known sessions eligible for late ASR results even if the live roster rolls over.
    for receipt in Path(directory).glob('*.json'):
        state=json.loads(receipt.read_text())
        old=state.get('slot') or dict(state['identity'],cell='历史排班')
        if config['room_groups'].get(old['room'])==old['group']:
            if state.get('grouping','session') == 'session':
                legacy_keys.add(key(old))
                # A legacy cross-midnight session must not also create daily split copies.
                for existing, part in list(selected.items()):
                    if part['room']==old['room'] and old['since']<=part['since'] and part['until']<=old['until']:
                        selected.pop(existing)
                selected[key(old)]=old
            else:
                selected.setdefault(key(old),old)
    from timeline_documents import assign_chapters
    selected={key(s):s for s in assign_chapters(selected.values())}
    results=[]
    for slot in sorted(selected.values(),key=lambda s:(s['since'],s['room'])):
        if slot['since']>now:continue
        rows=read_rows(database,slot['room'],slot['since'],slot['until'],by_start=grouping != 'session' and key(slot) not in legacy_keys)
        if not rows:continue
        slot=dict(slot,source=config['schedule_url'])
        try:
            result=sync.sync(slot,rows,target['base_token'],target['table_id'],now=now)
            results.append({'room':slot['room'],'since':slot['since'],'personnel':slot['personnel'],
                            'status':result.get('record_status',result['status']),
                            'url':result.get('url'),'segments':len(result.get('segments',{}))})
        except Exception as error:
            # CLI exceptions intentionally contain no raw credentials or responses.
            log.error('Session sync failed: %s %s %s',slot['room'],slot['since'],type(error).__name__)
            results.append({'room':slot['room'],'since':slot['since'],'status':'同步异常',
                            'error':str(error) if isinstance(error,(ValueError,RuntimeError)) else type(error).__name__})
    return results


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default='feishu_sessions.json')
    parser.add_argument('--database',default='data/state.sqlite')
    parser.add_argument('--state',default='data/feishu-sync')
    parser.add_argument('--interval',type=int,default=2)
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args()
    if args.interval<1:raise SystemExit('Interval must be at least 1 second')
    config=json.loads(Path(args.config).read_text())
    if config['folder_token']!=FOLDER:raise SystemExit('Document folder mismatch')
    # Reject missing mappings before making any remote writes.
    mapped_slots([],config['room_groups'],datetime.now(ZONE).date().isoformat())
    root=Path(args.state);root.mkdir(parents=True,exist_ok=True)
    stop=threading.Event()
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:stop.set())
    with (root/'service.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        schedule=None;refreshed=0;target=None;gateway=LiveGateway()
        while not stop.is_set():
            started=time.time()
            try:
                if schedule is None or started-refreshed>=300:
                    schedule=read_schedule(config);refreshed=started
                    target=call_base(['+url-resolve','--url',config['base_url']])
                    gateway.validate_table(target['base_token'],target['table_id'],schedule=True)
                results=run_once(config,args.database,root/'sessions',schedule,target,gateway)
                errors=sum(r['status']=='同步异常' for r in results)
                health={'at':time.time(),'ok':errors==0,'errors':errors,'sessions':results,'schedule_refreshed_at':refreshed}
            except Exception as error:
                log.error('Feishu sync loop failed: %s',type(error).__name__)
                health={'at':time.time(),'ok':False,'error':str(error) if isinstance(error,(ValueError,RuntimeError)) else type(error).__name__}
            atomic_json(root/'health.json',health)
            print(json.dumps(health,ensure_ascii=False),flush=True)
            if args.once:
                if not health['ok']:raise SystemExit(1)
                return
            stop.wait(args.interval)


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO)
    main()
