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


def test_waits_for_missing_transcription_before_later_rows(tmp_path):
    e,g=setup(tmp_path)
    result=e.sync(slot(),[row(),row('b',130,'queued'),row('c',150)],'base','table',now=170)
    assert list(result['segments'])==['a']
    assert g.update_record.call_args.args[-1]['转写状态']=='等待转写'
    e.sync(slot(),[row(),row('b',130),row('c',150)],'base','table',now=180)
    assert g.append_document.call_count==3


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
