from datetime import datetime
from duty_schedule import ZONE


def ts(hour, minute=0,day=21):
    return datetime(2026,9,day,hour,minute,tzinfo=ZONE).timestamp()


def slot(person='张三',start=8,end=12):
    return {'room':'live/taobao','personnel':person,'since':ts(start),'until':ts(end)}


def test_same_host_continuous_shifts_merge_but_returning_host_is_new_chapter():
    from timeline_documents import assign_chapters
    rows=[slot('张三',8,10),slot('张三',10,12),slot('李四',12,18),slot('张三',18,23)]
    result=assign_chapters(rows)
    assert [r['chapter_since'] for r in result]==[ts(8),ts(8),ts(12),ts(18)]
    assert result[0]['chapter_until']==result[1]['chapter_until']==ts(12)
    assert result[0]['until']==ts(10)  # Original Base session boundaries stay intact.


def test_midnight_and_gap_do_not_merge():
    from timeline_documents import assign_chapters,chapter_title
    rows=[slot('张三',8,10),slot('张三',12,14),dict(slot('王五',18,23),until=ts(0,day=22))]
    result=assign_chapters(rows)
    assert result[1]['chapter_since']==ts(12)
    assert chapter_title(result[-1])=='18:00–次日00:00 · 王五'


def test_earlier_chapter_uses_block_before_later_heading():
    from timeline_documents import insertion_anchor
    xml='<fragment><title id="title">当天文档</title><p id="intro">说明</p><h1 id="later">12:00–18:00 · 李四</h1></fragment>'
    assert insertion_anchor(xml,'later')=='intro'


def test_late_audio_is_inserted_before_later_text_in_its_chapter():
    from timeline_documents import segment_anchor
    xml='<fragment><h1 id="chapter">08:00–12:00 · 张三</h1><p id="t1"><b>2026-09-21T08:00:00.000000+08:00 · 45秒</b></p><p id="a">先到的第一段</p><p id="t3"><b>2026-09-21T10:00:00.000000+08:00 · 45秒</b></p><p id="c">先到的第三段</p></fragment>'
    assert segment_anchor(xml,ts(9))=='a'
    assert segment_anchor(xml,ts(7))=='chapter'
    assert segment_anchor(xml,ts(11))=='c'


def test_daily_sync_passes_shift_and_audio_time_to_gateway(tmp_path):
    from live_feishu_sync import LiveSessionSync
    from unittest.mock import Mock
    g=Mock();g.find_record.return_value=None;g.create_document.return_value={'document_id':'doc'};g.create_record.return_value='rec'
    shift=dict(slot(),group='天猫',source='https://example.com',cell='B4')
    audio={'id':'one','room':shift['room'],'start':ts(9),'duration':10,'provider':'feishu','state':'succeeded','text':'语音'}
    state=LiveSessionSync(tmp_path,g,grouping='daily').sync(shift,[audio],'base','table',now=ts(13))
    assert state['chapter_layout']=='timeline'
    assert g.append_shift.call_args.args[1:3]==(shift,audio)
    g.append_chapter.assert_not_called()


def test_chapters_do_not_merge_across_dates_or_rooms():
    from timeline_documents import assign_chapters
    rows=[dict(slot('张三',18,23),until=ts(0,day=22)),dict(slot('张三',0,8),since=ts(0,day=22),until=ts(8,day=22)),dict(slot('张三',18,23),room='live/other')]
    result=assign_chapters(rows)
    assert result[0]['chapter_until']==ts(0,day=22)
    assert result[1]['chapter_since']==ts(0,day=22)
    assert result[2]['chapter_until']==ts(23)


def test_earlier_chapter_fetches_preceding_blocks_before_insert():
    from live_feishu_sync import LiveGateway
    from unittest.mock import Mock, patch
    g=LiveGateway()
    g.fetch_document=Mock(side_effect=[
        '<fragment><h1 id="later">12:00–18:00 · 李四</h1></fragment>',
        '<title id="doc">当天</title><p id="intro">说明</p><h1 id="later">12:00–18:00 · 李四</h1><p id="text">下午</p>',
        '<fragment><h1 id="morning">08:00–12:00 · 张三</h1><h1 id="later">12:00–18:00 · 李四</h1></fragment>',
        '<fragment><h1 id="morning">08:00–12:00 · 张三</h1></fragment>'])
    with patch('export_feishu.cli') as call:
        g.append_shift('doc',slot(),{'start':ts(9)},'<p>上午</p>')
    assert call.call_args_list[0].args[0][-2:]==['--block-id','intro']
    assert call.call_args_list[1].args[0][-2:]==['--block-id','morning']
    assert g.fetch_document.call_args_list[1].args==('doc','--detail','with-ids')
