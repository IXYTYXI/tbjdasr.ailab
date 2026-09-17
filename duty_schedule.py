"""Parse the explicit group/date/time layout of the overall duty worksheet."""
import csv
import io
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ZONE = ZoneInfo('Asia/Shanghai')
GROUPS = {'天猫', '初中', '高中', '小学'}


def parse_schedule(data):
    if data.get('has_more'):
        raise ValueError('排班数据不完整')
    text = data['annotated_csv']
    numbers = [int(x) for x in re.findall(r'^\[row=(\d+)\] ', text, re.M)]
    rows = list(csv.reader(io.StringIO(re.sub(r'^\[row=\d+\] ', '', text, flags=re.M))))
    if len(rows) != len(numbers) or len(set(numbers)) != len(numbers):
        raise ValueError('排班行号结构异常')
    cols = data['col_indices']
    if cols[:8] != list('ABCDEFGH') and cols[:3] != list('ABC'):
        raise ValueError('排班必须从 A 列读取')
    anchors = {}
    for row in rows:
        for value in row:
            match = re.fullmatch(r'(20\d{2})/(\d{1,2})/(\d{1,2})', value.strip())
            if match:
                year, month, day = map(int, match.groups())
                anchors.setdefault((month, day), set()).add(year)
    result, group, dates = [], None, {}
    for number, row in zip(numbers, rows):
        first = row[0].strip() if row else ''
        if first in GROUPS | {'运营', '场控'}:
            group, dates = first, {}
            continue
        if first == '日期':
            dates = {}
            for col, value in enumerate(row[1:8], 1):
                match = re.fullmatch(r'(\d{1,2})月(\d{1,2})日', value.strip())
                if match:
                    month, day = map(int, match.groups())
                    years = anchors.get((month, day), set())
                    if len(years) != 1:
                        raise ValueError('无法从同表完整日期确定唯一年份')
                    dates[col] = date(next(iter(years)), month, day)
            continue
        match = re.fullmatch(r'(\d{1,2})[:：](\d{2})\s*[-~～—]\s*(\d{1,2})[:：](\d{2})', first)
        if group not in GROUPS or not match:
            continue
        if not dates:
            raise ValueError('直播班次缺少日期表头')
        h1, m1, h2, m2 = map(int, match.groups())
        if h1 >= 24 or h2 > 24 or m1 >= 60 or m2 >= 60 or (h2 == 24 and m2):
            raise ValueError('非法班次时间')
        for col, day in dates.items():
            person = row[col].strip() if col < len(row) else ''
            if not person:
                continue
            midnight = datetime.combine(day, datetime.min.time(), ZONE)
            start = midnight + timedelta(hours=h1, minutes=m1)
            end = midnight + timedelta(hours=h2, minutes=m2)
            if end <= start:
                end += timedelta(days=1)
            slot = dict(group=group, personnel=person, date=day.isoformat(),
                        since=start.timestamp(), until=end.timestamp(), cell=f'{cols[col]}{number}')
            for previous in result:
                if previous['group'] == group and previous['since'] < slot['until'] and slot['since'] < previous['until']:
                    raise ValueError('同组排班时间重叠，请核对来源表')
            result.append(slot)
    return result


def mapped_slots(slots, mapping, day):
    date.fromisoformat(day)
    if not mapping or any(group not in GROUPS for group in mapping.values()):
        raise ValueError('请先确认每路推流的排班分组映射；测试流请从映射中移除')
    return [dict(slot, room=room) for room, group in mapping.items()
            for slot in slots if slot['group'] == group and slot['date'] == day]
