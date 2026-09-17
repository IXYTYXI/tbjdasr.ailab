from unittest.mock import Mock
import pytest


def rows(state='succeeded'):
    return [dict(id='a',room='live/main',start=1,duration=2,provider='company',state=state,text='主播<讲话>')]


def exporter(tmp_path):
    from session_export import SessionExporter
    gateway=Mock()
    gateway.find_record.return_value=None
    gateway.create_document.return_value={'document_id':'doc1','url':'https://guanghe.feishu.cn/docx/doc1'}
    gateway.create_record.return_value='rec1'
    return SessionExporter(tmp_path,gateway),gateway


def run(e, data=None):
    return e.export('session-1','live/main',0,10,rows() if data is None else data,'直播测试','base1','tbl1')


def test_session_export_idempotent(tmp_path):
    e,g=exporter(tmp_path)
    a=run(e); b=run(e)
    assert a==b and a['record_id']=='rec1'
    g.create_document.assert_called_once()
    g.create_record.assert_called_once()
    fields=g.create_record.call_args.args[2]
    assert fields['场次编号']=='session-1'
    assert fields['转写文档']=='https://guanghe.feishu.cn/docx/doc1'
    assert '主播&lt;讲话&gt;' in g.append_document.call_args.args[1]


def test_incomplete_asr_blocks_export(tmp_path):
    e,g=exporter(tmp_path)
    with pytest.raises(ValueError,match='未完成'):
        run(e,rows('polling'))
    g.create_document.assert_not_called()


def test_unknown_append_outcome_is_not_repeated(tmp_path):
    e,g=exporter(tmp_path)
    g.append_document.side_effect=TimeoutError()
    with pytest.raises(TimeoutError):run(e)
    g.append_document.side_effect=None
    with pytest.raises(RuntimeError,match='不确定'):run(e)
    g.create_document.assert_called_once()
    g.append_document.assert_called_once()
    g.create_record.assert_not_called()


def test_record_unknown_outcome_is_not_duplicated(tmp_path):
    e,g=exporter(tmp_path)
    g.create_record.side_effect=TimeoutError()
    with pytest.raises(TimeoutError):run(e)
    g.create_record.side_effect=None
    with pytest.raises(RuntimeError,match='不确定'):run(e)
    g.create_record.assert_called_once()


def test_reusing_session_id_with_changed_content_requires_explicit_action(tmp_path):
    e,g=exporter(tmp_path);run(e)
    changed=rows();changed[0]['text']='different'
    with pytest.raises(ValueError,match='场次内容'):
        run(e,changed)
    g.create_document.assert_called_once()


def test_remote_session_detected_before_creating_document(tmp_path):
    e,g=exporter(tmp_path)
    g.find_record.return_value={'id':'existing'}
    with pytest.raises(ValueError,match='已有此场次'):
        run(e)
    g.create_document.assert_not_called()


def test_future_session_range_rejected(tmp_path):
    import time
    e,g=exporter(tmp_path)
    with pytest.raises(ValueError,match='尚未结束'):
        e.export('session-1','live/main',0,time.time()+600,rows(),'测试','base1','tbl1')
    g.create_document.assert_not_called()


def test_schedule_metadata_and_cross_boundary_segments(tmp_path):
    e,g=exporter(tmp_path)
    schedule={'group':'天猫','personnel':'甲<乙>','since':2,'until':10,'source':'https://example.com/wiki','cell':'B4'}
    e.export('schedule-1','live/main',2,10,rows(),'排班测试','base1','tbl1',schedule=schedule)
    fields=g.create_record.call_args.args[2]
    assert fields['直播人员']=='甲<乙>' and fields['排班单元格']=='B4'
    header=g.create_document.call_args.args[0]
    assert '甲&lt;乙&gt;' in header and '跨班' in header


def test_gateway_handles_cli_columnar_record_list(monkeypatch):
    import session_export
    monkeypatch.setattr(session_export,'call_base',lambda args:{'data':[],'record_id_list':[],'has_more':False})
    g=session_export.LarkGateway()
    assert g.find_record('base','table','session','room') is None
    monkeypatch.setattr(session_export,'call_base',lambda args:{'data':[['value']],'record_id_list':['recA'],'has_more':False})
    assert g.find_record('base','table','session','room')['id']=='recA'


def test_gateway_reads_back_id_when_create_envelope_omits_it(monkeypatch):
    import session_export
    monkeypatch.setattr(session_export,'call_base',lambda args:{'created':True,'record':{'add':{'场次编号':'s'}}})
    g=session_export.LarkGateway()
    g.find_record=Mock(side_effect=[None,{'id':'recB'}])
    assert g.create_record('base','table',{'场次编号':'s','直播间':'room'})=='recB'
