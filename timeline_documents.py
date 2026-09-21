"""Chronological daily chapters based on contiguous roster assignments."""
from datetime import datetime
import re
import xml.etree.ElementTree as ET
from duty_schedule import ZONE


def day_at(value):
    return datetime.fromtimestamp(value, ZONE).date()


def assign_chapters(slots):
    result = [dict(s) for s in slots]
    groups = {}
    for item in result:
        groups.setdefault((item['room'], day_at(item['since'])), []).append(item)
    for items in groups.values():
        items.sort(key=lambda s: (s['since'], s['until']))
        runs = []
        for item in items:
            if runs and runs[-1][-1]['until'] == item['since'] and runs[-1][-1]['personnel'] == item['personnel']:
                runs[-1].append(item)
            else:
                runs.append([item])
        for run in runs:
            for item in run:
                item.update(chapter_since=run[0]['since'], chapter_until=run[-1]['until'])
    return result


def chapter_title(slot):
    start = datetime.fromtimestamp(slot.get('chapter_since', slot['since']), ZONE)
    end = datetime.fromtimestamp(slot.get('chapter_until', slot['until']), ZONE)
    fmt = '%H:%M:%S' if start.second or end.second else '%H:%M'
    until = ('次日' if end.date() > start.date() else '') + end.strftime(fmt)
    return start.strftime(fmt) + '–' + until + ' · ' + slot['personnel']


def chapter_order(title):
    match = re.match(r'^(\d{2}):(\d{2})(?::(\d{2}))?–', title)
    if not match:
        return None
    h, m, s = match.groups()
    return int(h)*3600 + int(m)*60 + int(s or 0)


def blocks(xml):
    root = ET.fromstring('<root>'+xml+'</root>')
    container = root.find('fragment')
    if container is None:
        container = root
    return [node for node in container if node.attrib.get('id')]


def insertion_anchor(xml, before_id):
    previous = None
    for node in blocks(xml):
        if node.attrib['id'] == before_id:
            if previous is None:
                raise RuntimeError('章节前没有可用插入位置')
            return previous
        previous = node.attrib['id']
    raise RuntimeError('无法定位后续章节')


def segment_anchor(xml, start):
    previous = None
    for node in blocks(xml):
        if node.tag == 'p' and node.find('b') is not None:
            text = ''.join(node.itertext()).split(' · ')[0]
            try:
                value = datetime.fromisoformat(text)
                timestamp = value.timestamp() if value.tzinfo is not None else None
            except ValueError:
                timestamp = None
            if timestamp is not None and timestamp > start:
                if previous is None:
                    raise RuntimeError('录音片段前没有可用插入位置')
                return previous
        previous = node.attrib['id']
    if previous is None:
        raise RuntimeError('章节没有可用插入位置')
    return previous
