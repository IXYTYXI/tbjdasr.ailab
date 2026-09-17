import pytest
from datetime import datetime
from zoneinfo import ZoneInfo


def sample():
    return {'annotated_csv':'[row=1] 天猫,,,,\n[row=2] 日期,9月17日,9月18日,,\n[row=3] ,周四,周五,,\n[row=4] 20:00-24:00,甲,乙,,\n[row=5] 初中,,,,\n[row=6] 日期,9月17日,9月18日,,\n[row=7] 8:00-12:00,丙,,,\n[row=8] 运营,,,,\n[row=9] 日期,2026/9/17,2026/9/18,,\n[row=10] 8：00-16：00,丁,戊,,', 'row_indices':list(range(1,11)), 'col_indices':['A','B','C','D','E'], 'has_more':False}


def test_parses_actual_group_people_midnight_and_source_cell():
    from duty_schedule import parse_schedule
    slots=parse_schedule(sample())
    assert len(slots)==3
    a=slots[0]
    assert (a['group'],a['personnel'],a['cell'])==('天猫','甲','B4')
    zone=ZoneInfo('Asia/Shanghai')
    assert datetime.fromtimestamp(a['until'],zone).isoformat()=='2026-09-18T00:00:00+08:00'
    assert a['until']-a['since']==14400


def test_no_guessed_year_or_partial_results():
    from duty_schedule import parse_schedule
    data=sample();data['annotated_csv']=data['annotated_csv'].replace('2026/9/17','9月17日').replace('2026/9/18','9月18日')
    with pytest.raises(ValueError,match='年份'):parse_schedule(data)
    data=sample();data['has_more']=True
    with pytest.raises(ValueError,match='完整'):parse_schedule(data)


def test_unknown_stream_mapping_cannot_export():
    from duty_schedule import mapped_slots,parse_schedule
    with pytest.raises(ValueError,match='映射'):mapped_slots(parse_schedule(sample()),{'live/taobao':None},'2026-09-17')
    assert len(mapped_slots(parse_schedule(sample()),{'live/taobao':'天猫'},'2026-09-17'))==1


def test_rejects_overlapping_duties():
    from duty_schedule import parse_schedule
    data=sample()
    data['annotated_csv'] += '\n[row=11] 天猫,,,,\n[row=12] 日期,9月17日,9月18日,,\n[row=13] 21:00-23:00,其他人,,,'
    with pytest.raises(ValueError,match='重叠'):parse_schedule(data)


def test_fetch_jobs_paginates_by_all_fetched_rows_not_overlap_count():
    from scheduled_export import fetch_jobs
    from unittest.mock import Mock
    client=Mock()
    first=[{'start':0,'duration':30} for _ in range(999)]+[{'start':90,'duration':30}]
    client.get.side_effect=[Mock(json=lambda:first),Mock(json=lambda:[{'start':110,'duration':30}])]
    assert len(fetch_jobs(client,'http://localhost','live/taobao',100,200))==2
    assert client.get.call_args_list[1].kwargs['params']['offset']==1000


def test_one_conflicted_session_does_not_block_later_sessions(tmp_path,monkeypatch,capsys):
    import json,sys
    import scheduled_export as module
    from unittest.mock import Mock
    config=tmp_path/'config.json'
    config.write_text(json.dumps({'folder_token':module.FOLDER,'room_groups':{'live/taobao':'天猫'},'base_url':'url','schedule_url':'source'}))
    monkeypatch.setattr(sys,'argv',['export','--config',str(config),'--date','2026-09-17','--apply'])
    monkeypatch.setenv('API_KEY','test')
    monkeypatch.setattr(module,'read_schedule',lambda config:[])
    slot={'room':'live/taobao','group':'天猫','personnel':'甲','since':1,'until':3,'cell':'B4'}
    monkeypatch.setattr(module,'mapped_slots',lambda *args:[slot,dict(slot,since=3,until=5,cell='B5')])
    monkeypatch.setattr(module,'call_base',lambda args:{'base_token':'base','table_id':'table'})
    monkeypatch.setattr(module,'fetch_jobs',lambda *args:[{'state':'succeeded'}])
    exporter=Mock();exporter.export.side_effect=[ValueError('旧场次有冲突'),{'status':'done'}]
    monkeypatch.setattr(module,'SessionExporter',lambda *args:exporter)
    module.main()
    data=json.loads(capsys.readouterr().out)
    assert len(data)==2 and data[0]['status']=='归档失败，待核对' and data[1]['status']=='done'
