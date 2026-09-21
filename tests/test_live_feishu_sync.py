from unittest.mock import Mock
import pytest


def setup(tmp_path):
    from live_feishu_sync import LiveSessionSync
    gateway=Mock();gateway.find_record.return_value=None
    gateway.create_document.return_value={'document_id':'doc1','url':'https://guanghe.feishu.cn/docx/doc1'}
    gateway.create_record.return_value='rec1'
    gateway.fetch_document.return_value=''
    return LiveSessionSync(tmp_path,gateway),gateway


def slot():
    return {'room':'live/taobao','group':'天猫','personnel':'甲','since':100.,'until':200.,'cell':'B4','source':'https://example.com/schedule'}


def row(id='a',start=110,state='succeeded'):
    return {'id':id,'room':'live/taobao','start':start,'duration':10,'state':state,'text':'这是一段<文字>','provider':'feishu'}


def test_incremental_restart_one_document_and_one_record(tmp_path):
    from live_feishu_sync import LiveSessionSync
    e,g=setup(tmp_path)
    first=e.sync(slot(),[row()], 'base','table',now=150)
    again=LiveSessionSync(tmp_path,g).sync(slot(),[row(),row('b',130)],'base','table',now=160)
    assert first['document_id']==again['document_id']
    assert len(again['segments'])==2
    g.create_document.assert_called_once();g.create_record.assert_called_once()
    assert g.append_document.call_count==2


def test_later_success_is_synced_while_earlier_waits(tmp_path):
    e,g=setup(tmp_path)
    result=e.sync(slot(),[row(),row('b',130,'queued'),row('c',150)],'base','table',now=170)
    assert list(result['segments'])==['a','c']
    assert g.update_record.call_args.args[-1]['转写状态']=='等待转写'
    e.sync(slot(),[row(),row('b',130),row('c',150)],'base','table',now=180)
    assert g.append_document.call_count==3
    assert '补录片段' in g.append_document.call_args.args[1]


def test_lost_append_response_readback_prevents_duplicate(tmp_path):
    e,g=setup(tmp_path)
    def append(doc,xml):
        g.fetch_document.return_value=xml
        raise TimeoutError()
    g.append_document.side_effect=append
    with pytest.raises(TimeoutError):e.sync(slot(),[row()],'base','table',now=150)
    g.append_document.side_effect=None
    result=e.sync(slot(),[row()],'base','table',now=160)
    assert len(result['segments'])==1
    assert g.append_document.call_count==1


def test_uncertain_missing_content_blocks_blind_retry(tmp_path):
    e,g=setup(tmp_path);g.append_document.side_effect=TimeoutError()
    with pytest.raises(TimeoutError):e.sync(slot(),[row()],'base','table',now=150)
    g.append_document.side_effect=None
    with pytest.raises(RuntimeError,match='核对'):e.sync(slot(),[row()],'base','table',now=160)
    assert g.append_document.call_count==1


def test_changed_schedule_or_transcript_needs_review(tmp_path):
    e,g=setup(tmp_path);e.sync(slot(),[row()],'base','table',now=150)
    with pytest.raises(ValueError,match='排班'):
        e.sync(dict(slot(),personnel='乙'),[row()],'base','table',now=160)
    with pytest.raises(ValueError,match='文字'):
        e.sync(slot(),[dict(row(),text='被修改')],'base','table',now=160)


def test_ended_session_does_not_claim_full_recording_coverage(tmp_path):
    e,g=setup(tmp_path);e.sync(slot(),[row()],'base','table',now=300)
    assert g.update_record.call_args.args[-1]['转写状态']=='已同步现有录音'


def test_failed_segment_does_not_block_later_success_and_can_recover(tmp_path):
    e,g=setup(tmp_path)
    result=e.sync(slot(),[row('a',110,'failed'),row('b',130)],'base','table',now=300)
    assert list(result['segments'])==['b']
    assert result['record_status']=='有失败片段'
    result=e.sync(slot(),[row('a',110),row('b',130)],'base','table',now=310)
    assert set(result['segments'])=={'a','b'}
    assert '补录片段' in g.append_document.call_args.args[1]


def test_runner_keeps_persisted_session_after_schedule_removed(tmp_path):
    from feishu_sync_service import run_once
    from unittest.mock import patch
    e,g=setup(tmp_path/'sessions')
    e.sync(slot(),[row()], 'base','table',now=150)
    config={'room_groups':{'live/taobao':'天猫'},'schedule_url':slot()['source']}
    with patch('feishu_sync_service.read_rows',return_value=[row(),row('b',130)]):
        result=run_once(config,'unused',tmp_path/'sessions',[],{'base_token':'base','table_id':'table'},g,now=300)
    assert len(result)==1 and result[0]['segments']==2
    g.create_document.assert_called_once()


def test_unchanged_session_skips_remote_calls_but_closure_updates_status(tmp_path):
    e,g=setup(tmp_path)
    e.sync(slot(),[row()],'base','table',now=150)
    count=g.validate_table.call_count
    e.sync(slot(),[row()],'base','table',now=160)
    assert g.validate_table.call_count==count
    e.sync(slot(),[row()],'base','table',now=210)
    assert g.update_record.call_args.args[-1]['转写状态']=='已同步现有录音'


def test_daily_person_shares_document_across_sessions_after_restart(tmp_path):
    from live_feishu_sync import LiveSessionSync
    _,g=setup(tmp_path)
    first=LiveSessionSync(tmp_path,g,grouping='daily_person').sync(slot(),[row()], 'base','table',now=300)
    other=dict(slot(),since=300.,until=400.)
    second=LiveSessionSync(tmp_path,g,grouping='daily_person').sync(other,[row('b',310)], 'base','table',now=500)
    assert first['document_id']==second['document_id']
    assert g.create_document.call_count==1
    assert g.create_record.call_count==2
    assert g.append_shift.call_count==2
    assert g.append_shift.call_args.args[1]['personnel']=='甲'


def test_daily_person_separates_people_and_dates(tmp_path):
    from live_feishu_sync import LiveSessionSync
    _,g=setup(tmp_path)
    sync=LiveSessionSync(tmp_path,g,grouping='daily_person')
    sync.sync(slot(),[row()], 'base','table',now=300)
    sync.sync(dict(slot(),personnel='乙',since=300.,until=400.),[row('b',310)],'base','table',now=500)
    sync.sync(dict(slot(),since=86500.,until=86600.),[row('c',86510)],'base','table',now=87000)
    assert g.create_document.call_count==3


def test_daily_document_can_include_different_people(tmp_path):
    from live_feishu_sync import LiveSessionSync
    _,g=setup(tmp_path)
    sync=LiveSessionSync(tmp_path,g,grouping='daily')
    sync.sync(slot(),[row()], 'base','table',now=300)
    sync.sync(dict(slot(),personnel='乙',since=300.,until=400.),[row('b',310)],'base','table',now=500)
    assert g.create_document.call_count==1
    assert g.append_shift.call_args.args[1]['personnel']=='乙'


def test_daily_create_uncertain_blocks_another_session(tmp_path):
    from live_feishu_sync import LiveSessionSync
    _,g=setup(tmp_path);g.create_document.side_effect=TimeoutError()
    sync=LiveSessionSync(tmp_path,g,grouping='daily_person')
    with pytest.raises(TimeoutError):sync.sync(slot(),[row()], 'base','table',now=300)
    g.create_document.side_effect=None
    with pytest.raises(RuntimeError,match='不确定'):
        sync.sync(dict(slot(),since=300.,until=400.),[row('b',310)],'base','table',now=500)
    assert g.create_document.call_count==1


def test_midnight_shift_is_split_at_beijing_day_boundary():
    from datetime import datetime
    from duty_schedule import ZONE
    from daily_documents import calendar_slots
    start=datetime(2026,9,21,23,30,tzinfo=ZONE).timestamp()
    parts=list(calendar_slots(dict(slot(),since=start,until=start+7200)))
    assert [p['date'] for p in parts]==['2026-09-21','2026-09-22']
    assert parts[0]['until']==parts[1]['since']
    assert parts[0]['personnel']==parts[1]['personnel']=='甲'


def test_append_chapter_uses_existing_chapter_tail_not_document_end():
    from live_feishu_sync import LiveGateway
    from unittest.mock import patch
    g=LiveGateway()
    g.fetch_document=Mock(side_effect=[
        '<fragment><h1 id="a">主播：甲</h1><h1 id="b">主播：乙</h1></fragment>',
        '<fragment><h1 id="a">主播：甲</h1><p id="last-a">先前文字</p></fragment>'])
    with patch('export_feishu.cli') as call:
        g.append_chapter('doc','甲','<p>稍后回到甲的场次</p>')
    assert call.call_args.args[0][-2:]==['--block-id','last-a']


def test_daily_select_assigns_cross_shift_audio_only_once(tmp_path):
    import sqlite3
    from feishu_sync_service import read_rows
    db=tmp_path/'test.sqlite'
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE jobs(id,room,start,duration,provider,state,text)')
        c.execute("INSERT INTO jobs VALUES ('a','live/taobao',195,10,'feishu','succeeded','交班文字')")
    first=read_rows(db,'live/taobao',100,200,by_start=True)
    second=read_rows(db,'live/taobao',200,300,by_start=True)
    assert len(first)==1 and second==[]


def test_daily_mode_keeps_legacy_cross_midnight_receipt_without_duplicate(tmp_path):
    from feishu_sync_service import run_once
    from datetime import datetime
    from duty_schedule import ZONE
    from unittest.mock import patch
    start=datetime(2026,9,21,23,30,tzinfo=ZONE).timestamp()
    shift=dict(slot(),since=start,until=start+7200,date='2026-09-21')
    r=row(start=start+10)
    e,g=setup(tmp_path/'sessions');e.sync(shift,[r],'base','table',now=start+8000)
    config={'room_groups':{'live/taobao':'天猫'},'schedule_url':shift['source'],'document_grouping':'daily'}
    with patch('feishu_sync_service.read_rows',return_value=[r]):
        result=run_once(config,'unused',tmp_path/'sessions',[shift],{'base_token':'base','table_id':'table'},g,now=start+8000)
    assert len(result)==1
    g.create_document.assert_called_once()


def test_persistent_test_label_survives_new_transcript_sync(tmp_path):
    from live_feishu_sync import atomic_json
    import json
    e,g=setup(tmp_path);e.sync(slot(),[row()],'base','table',now=150)
    path=next(tmp_path.glob('*.json'));state=json.loads(path.read_text());state['test_only']=True;atomic_json(path,state)
    e.sync(slot(),[row(),row('b',130)],'base','table',now=160)
    fields=g.update_record.call_args.args[-1]
    assert fields['场次名称'].startswith('【测试】')
    assert fields['转写状态'].startswith('【测试】')


def test_production_cutover_isolates_old_receipts_rows_and_record_identity(tmp_path):
    from feishu_sync_service import run_once
    from unittest.mock import patch
    e,g=setup(tmp_path/'sessions')
    old=e.sync(slot(),[row()], 'base','table',now=150)
    g.reset_mock()
    config={'room_groups':{'live/taobao':'天猫'},'schedule_url':slot()['source'],
            'archive_id':'production-1','archive_since':125}
    shift=dict(slot(),date='1970-01-01')
    with patch('feishu_sync_service.read_rows',return_value=[row(),row('new',130)]):
        result=run_once(config,'unused',tmp_path/'sessions',[shift],{'base_token':'base','table_id':'table'},g,now=160)
    assert result[0]['segments']==1
    assert g.create_record.call_count==1
    assert g.create_record.call_args.args[-1]['场次编号']!=old['session_id']
    assert list((tmp_path/'sessions'/'archives'/'production-1').glob('*.json'))
    with patch('feishu_sync_service.read_rows',return_value=[row()]):
        assert run_once(config,'unused',tmp_path/'sessions',[shift],{'base_token':'base','table_id':'table'},g,now=160)==[]


def test_completed_audio_in_open_shift_updates_legacy_status_without_new_audio(tmp_path):
    import json
    from live_feishu_sync import digest, atomic_json
    e,g=setup(tmp_path)
    s=slot(); rows=[row()]
    e.sync(s,rows,'base','table',now=150)
    path=next(tmp_path.glob('*.json'));state=json.loads(path.read_text())
    state['record_status']='同步中'
    state['snapshot_hash']=digest({'rows':rows,'slot':s,'ended':False,'test_only':False})
    state['fields_digest']='old-status'
    atomic_json(path,state);g.reset_mock()
    result=e.sync(s,rows,'base','table',now=160)
    assert result['record_status']=='现有录音已同步，继续录制中'
    assert g.update_record.call_args.args[-1]['转写状态']=='现有录音已同步，继续录制中'
    g.append_document.assert_not_called()
