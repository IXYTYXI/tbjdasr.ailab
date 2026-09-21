"""Durable incremental Feishu documents, one document per scheduled session."""
from lark_runtime import command
import fcntl
import hashlib
from html import escape
import json
import os
from pathlib import Path
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from duty_schedule import ZONE
from export_feishu import FOLDER
from session_export import LarkGateway, call_base
import subprocess


def stamp(value):
    return datetime.fromtimestamp(value, ZONE).strftime('%Y-%m-%d %H:%M:%S')


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def atomic_json(path, data):
    path = Path(path);path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2);f.flush();os.fsync(f.fileno())
    temporary.replace(path)


def segment_xml(row, late=False):
    # The full timestamp is both useful to readers and unique in the selected source.
    title = datetime.fromtimestamp(row['start'], ZONE).isoformat(timespec='microseconds')
    title += f" · {row['duration']:.3f} 秒" + (' · 补录片段' if late else '')
    text = escape(row['text'] or '（未识别到文字）').replace('\n','<br/>')
    return f'<p><b>{title}</b></p><p>{text}</p>'


def contains_fragment(document, fragment):
    def paragraphs(xml):
        root = ET.fromstring('<root>'+xml+'</root>')
        return [''.join(p.itertext()) for p in root.iter('p')]
    try:
        actual, expected = paragraphs(document), paragraphs(fragment)
    except ET.ParseError:
        return False
    return bool(expected) and any(actual[i:i+len(expected)] == expected for i in range(len(actual)))


class LiveGateway(LarkGateway):
    def fetch_document(self, document_id, *options):
        result = subprocess.run(command('docs','+fetch','--doc',document_id,'--as','user','--format','json',*options),
                                capture_output=True, text=True, timeout=180)
        data = json.loads(result.stdout)
        if result.returncode or not data.get('ok') or data.get('identity') != 'user':
            raise RuntimeError('无法读取飞书文档以核对写入结果')
        return data['data']['document']['content']

    def append_chapter(self, document_id, personnel, fragment):
        from export_feishu import cli
        title = '主播：' + personnel
        def heading():
            content = self.fetch_document(document_id, '--scope', 'outline', '--detail', 'with-ids')
            root = ET.fromstring('<root>'+content+'</root>')
            matches = [n.attrib.get('id') for n in root.iter('h1') if ''.join(n.itertext()) == title]
            if len(matches) > 1:
                raise RuntimeError('主播章节重复，请核对文档')
            return matches[0] if matches else None
        anchor = heading()
        if not anchor:
            self.append_document(document_id, '<h1>'+escape(title)+'</h1>')
            anchor = heading()
        if not anchor:
            raise RuntimeError('无法定位主播章节')
        content = self.fetch_document(document_id, '--scope', 'section', '--start-block-id', anchor, '--detail', 'with-ids')
        root = ET.fromstring('<root>'+content+'</root>')
        blocks = [n for n in root.iter() if n.tag in ('h1','p') and n.attrib.get('id')]
        if not blocks:
            raise RuntimeError('主播章节没有可用插入位置')
        cli(['+update','--doc',document_id,'--command','block_insert_after','--block-id',blocks[-1].attrib['id']], fragment)

    def append_shift(self, document_id, slot, row, fragment):
        from export_feishu import cli
        from timeline_documents import chapter_title, chapter_order, insertion_anchor, segment_anchor
        title=chapter_title(slot)
        def outline():
            xml=self.fetch_document(document_id,'--scope','outline','--detail','with-ids')
            return [(n.attrib.get('id'),''.join(n.itertext())) for n in ET.fromstring('<root>'+xml+'</root>').iter('h1')]
        headings=outline()
        matches=[block for block,text in headings if text==title]
        if len(matches)>1:
            raise RuntimeError('排班章节重复，请核对文档')
        anchor=matches[0] if matches else None
        if not anchor:
            later=next((block for block,text in headings if chapter_order(text) is not None and chapter_order(text)>chapter_order(title)),None)
            heading='<h1>'+escape(title)+'</h1>'
            if later:
                # The API treats an end-only range as a single block. Read IDs to locate
                # the preceding top-level block when inserting an earlier chapter.
                xml=self.fetch_document(document_id,'--detail','with-ids')
                before=insertion_anchor(xml,later)
                cli(['+update','--doc',document_id,'--command','block_insert_after','--block-id',before],heading)
            else:
                self.append_document(document_id,heading)
            anchor=next((block for block,text in outline() if text==title),None)
        if not anchor:
            raise RuntimeError('无法定位排班章节')
        xml=self.fetch_document(document_id,'--scope','section','--start-block-id',anchor,'--detail','with-ids')
        after=segment_anchor(xml,row['start'])
        cli(['+update','--doc',document_id,'--command','block_insert_after','--block-id',after],fragment)

    def update_record(self, base, table, record_id, fields):
        data = call_base(['+record-upsert','--base-token',base,'--table-id',table,'--record-id',record_id,
                          '--json',json.dumps(fields,ensure_ascii=False)])
        if not data.get('updated') or data.get('ignored_fields'):
            raise RuntimeError('场次记录更新未确认成功')


class LiveSessionSync:
    def __init__(self, directory, gateway, grouping="session", chapter_layout="timeline"):
        self.directory = Path(directory);self.directory.mkdir(parents=True,exist_ok=True)
        self.gateway = gateway
        if grouping not in ("session", "daily", "daily_person"):
            raise ValueError("Unknown document grouping")
        self.grouping = grouping
        if chapter_layout not in ("host", "timeline"):
            raise ValueError("Unknown chapter layout")
        self.chapter_layout = chapter_layout

    def sync(self, slot, rows, base, table, now=None):
        now = time.time() if now is None else now
        if not slot['since'] < slot['until'] or not slot['personnel']:
            raise ValueError('排班时间或人员无效')
        rows = sorted(rows,key=lambda r:(r['start'],r['id']))
        if len({r['id'] for r in rows}) != len(rows):
            raise ValueError('重复的音频任务')
        for r in rows:
            if r['room'] != slot['room'] or r['start'] >= slot['until'] or r['start']+r['duration'] <= slot['since']:
                raise ValueError('音频不属于此场次')
        if not rows:
            return {'status':'暂无音频'}
        session_parts=[slot['room'],slot['since'],slot['until']]
        if slot.get('archive_id'):
            session_parts.append(slot['archive_id'])
        session = 'duty-'+hashlib.sha256(json.dumps(session_parts).encode()).hexdigest()[:20]
        identity = {k:slot[k] for k in ('room','group','personnel','since','until','source')}
        key = digest([session,base,table,FOLDER]);path=self.directory/(key+'.json')
        with (self.directory/(key+'.lock')).open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            state = json.loads(path.read_text()) if path.exists() else {'session_id':session,'identity':identity,'segments':{},'status':'new'}
            if state['identity'] != identity:
                raise ValueError('排班信息已改变，请核对已有场次，不自动重复创建')
            def save(**values):
                state.update(values);atomic_json(path,state)
            for row in rows:
                if row['id'] in state['segments'] and state['segments'][row['id']] != digest(row):
                    raise ValueError('已同步文字或片段信息发生变化，请核对')
            if state['status']=='create_uncertain':
                raise RuntimeError('创建文档结果不确定，请核对后修复回执')
            snapshot_hash = digest({'rows':rows,'slot':slot,'ended':now >= slot['until'],'test_only':state.get('test_only',False)})
            if state.get('snapshot_hash') == snapshot_hash and state['status'] == 'ready' and not state.get('pending'):
                return state
            self.gateway.validate_table(base,table,schedule=True)
            title=f"{stamp(slot['since'])[:16]} {slot['group']} {slot['personnel']} 直播转写"
            if state.get('test_only'):
                title='【测试】'+title
            if state['status']=='new':
                if self.gateway.find_record(base,table,session,slot['room']):
                    raise RuntimeError('场次已存在，请恢复其回执后核对，不能重复建文档')
                header=f'<title>{escape(title)}</title><p>直播人员（按排班）：{escape(slot["personnel"])}；排班：{stamp(slot["since"])} 至 {stamp(slot["until"])}（北京时间）。</p>'
                header+='<p>本文持续追加已识别的录音。片段时间用于定位；跨班片段可能包含交班前后人员。是否已同步及转写异常请查看场次表；已同步现有录音不代表整场采集完整。</p>'
                header+=f'<p>排班来源：<a href="{escape(slot["source"],quote=True)}">直播排班表</a>；分组：{escape(slot["group"])}。</p>'
                save(status='create_uncertain')
                if self.grouping == 'session':
                    doc=self.gateway.create_document(header)
                else:
                    from daily_documents import DailyDocuments
                    doc=DailyDocuments(self.directory,self.gateway).get(slot,self.grouping,base,table,chapter_layout=self.chapter_layout)
                state['grouping']=self.grouping
                state['chapter_layout']=self.chapter_layout
                save(status='ready',document_id=doc['document_id'],url=doc.get('url') or 'https://guanghe.feishu.cn/docx/'+doc['document_id'])
            fields={'场次编号':session,'场次名称':title,'直播间':slot['room'],'排班分组':slot['group'],
                    '直播人员':slot['personnel'],'排班开始':stamp(slot['since']),'排班结束':stamp(slot['until']),
                    '开始时间':stamp(min(r['start'] for r in rows)),'结束时间':stamp(max(r['start']+r['duration'] for r in rows)),
                    '转写文档':state['url'],'排班来源':slot['source'],'排班单元格':slot['cell'],'转写状态':'同步中'}
            if state.get('record_pending'):
                found=self.gateway.find_record(base,table,session,slot['room'])
                if not found:
                    raise RuntimeError('创建场次记录结果不确定，请核对')
                save(record_id=found.get('id') or found.get('record_id'),record_pending=False)
            if not state.get('record_id'):
                save(record_pending=True)
                record_id=self.gateway.create_record(base,table,fields)
                save(record_id=record_id,record_pending=False)
            save(slot=slot)
            pending=state.get('pending')
            if pending:
                content=self.gateway.fetch_document(state['document_id'])
                if not contains_fragment(content,pending['xml']):
                    raise RuntimeError('追加内容结果不确定，请核对文档与回执，暂不重复追加')
                state['segments'][pending['id']]=pending['digest']
                save(pending=None,last_start=max(state.get('last_start',0),pending['start']))
            for row in rows:
                if row['id'] in state['segments']:
                    continue
                if row['state']=='failed':
                    continue  # Keep the failure visible in the index; recovered audio is appended later.
                if row['state']!='succeeded':
                    continue  # Publish later final results; earlier audio is backfilled with a label.
                fragment=segment_xml(row,late=row['start']<state.get('last_start',0))
                save(pending={'id':row['id'],'digest':digest(row),'xml':fragment,'start':row['start']})
                if state.get('grouping', 'session') == 'session':
                    self.gateway.append_document(state['document_id'],fragment)
                elif state.get('chapter_layout','host') == 'timeline':
                    self.gateway.append_shift(state['document_id'],slot,row,fragment)
                else:
                    self.gateway.append_chapter(state['document_id'],slot['personnel'],fragment)
                state['segments'][row['id']]=digest(row)
                save(pending=None,last_start=max(state.get('last_start',0),row['start']))
            if any(r['state']=='failed' for r in rows):fields['转写状态']='有失败片段'
            elif any(r['state']!='succeeded' for r in rows):fields['转写状态']='等待转写'
            elif now < slot['until']:fields['转写状态']='同步中'
            else:fields['转写状态']='已同步现有录音'
            if state.get('test_only'):
                fields['转写状态']='【测试】'+fields['转写状态']
            if state.get('fields_digest') != digest(fields):
                self.gateway.update_record(base,table,state['record_id'],fields)
                save(fields_digest=digest(fields))
            save(status='ready',synced_at=now,record_status=fields['转写状态'],snapshot_hash=snapshot_hash)
            return state
