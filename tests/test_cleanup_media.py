import hashlib
import json
from pathlib import Path
import pytest
from app.store import Store
from live_feishu_sync import digest


def fixture(tmp):
    data=tmp/'data';data.mkdir();db=Store(data/'state.sqlite')
    marker=data/'recordings/live/taobao/100-000000.ready.json';marker.parent.mkdir(parents=True)
    source=marker.with_name('100-000000.mp4');source.write_bytes(b'video')
    marker.write_text(json.dumps({'path':'/data/recordings/live/taobao/100-000000.mp4','room':'live/taobao'}))
    aid=hashlib.sha256(str(marker.relative_to(data/'recordings')).encode()).hexdigest()
    audio=data/'audio'/aid/'parts/00000.wav';audio.parent.mkdir(parents=True);audio.write_bytes(b'audio')
    db.asset(aid,'/data/'+str(marker.relative_to(data)),state='done')
    job=dict(id=aid+'_00000',room='live/taobao',path=str(audio.relative_to(data)),start=100.,duration=45.,provider='company')
    db.add_job(job);db.update(job['id'],state='succeeded',text='正文')
    row={k:db.get(job['id'])[k] for k in ('id','room','start','duration','provider','state','text')}
    receipts=data/'receipts';receipts.mkdir()
    receipt=receipts/'session.json'
    receipt.write_text(json.dumps({'status':'ready','record_id':'rec1','document_id':'doc1','synced_at':0,'segments':{job['id']:digest(row)}}))
    return data,db,source,audio,receipt,job


def test_preview_apply_and_repeat_preserve_text(tmp_path):
    from cleanup_media import cleanup
    data,db,source,audio,receipt,job=fixture(tmp_path)
    result=cleanup(data,receipt.parent,retention_hours=0)
    assert result['eligible']==1 and source.exists() and audio.exists()
    result=cleanup(data,receipt.parent,retention_hours=0,apply=True)
    assert result['deleted_files']==2 and not source.exists() and not audio.exists()
    assert db.get(job['id'])['text']=='正文'
    assert cleanup(data,receipt.parent,retention_hours=0,apply=True)['deleted_files']==0


@pytest.mark.parametrize('reason',['pending','failed','missing_receipt','uncertain','changed_text','test','active','retention','symlink','missing_job'])
def test_unsafe_or_unfinished_assets_kept(tmp_path,reason):
    from cleanup_media import cleanup
    data,db,source,audio,receipt,job=fixture(tmp_path)
    s=json.loads(receipt.read_text());hours=0
    if reason in ('pending','failed'):db.update(job['id'],state='polling' if reason=='pending' else 'failed')
    elif reason=='missing_receipt':s['segments']={}
    elif reason=='uncertain':s['pending']={'id':job['id']}
    elif reason=='changed_text':db.update(job['id'],text='changed')
    elif reason=='test':s['test_only']=True
    elif reason=='active':source.with_suffix('.ready.json').unlink()
    elif reason=='retention':hours=24
    elif reason=='missing_job':(audio.parent/'00001.wav').write_bytes(b'unknown')
    elif reason=='symlink':
        outside=tmp_path/'outside.wav';outside.write_bytes(b'outside');audio.unlink();audio.symlink_to(outside)
    receipt.write_text(json.dumps(s))
    assert cleanup(data,receipt.parent,retention_hours=hours,apply=True)['deleted_files']==0
    assert source.exists() and audio.exists()
