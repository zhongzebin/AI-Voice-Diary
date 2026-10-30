"""日记存储服务 - 每天一个Markdown文件 (diaries/YYYY-MM-DD.md)"""
import re
import json
import uuid
import threading
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Optional

from ..models import DiaryEntryMetadata

WEEKDAYS = ['一', '二', '三', '四', '五', '六', '日']
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
SECTION_SPLIT = re.compile(r'(?m)^(?=### 🕐 )')
META_RE = re.compile(r'<!-- META: (.+?) -->')
TIME_RE = re.compile(r'^### 🕐 (\d{2}:\d{2})')
SUMMARY_RE = re.compile(r'\*\*总结\*\*:\s*(.+?)(?=\n\s*\n\*\*标签\*\*|\n\s*\*\*标签\*\*|\n---|\Z)', re.DOTALL)


def validate_date(date_str: str) -> str:
    """校验日期格式,防止路径穿越"""
    if not DATE_RE.match(date_str or ""):
        raise ValueError(f"日期格式错误: {date_str}")
    datetime.strptime(date_str, '%Y-%m-%d')
    return date_str


class DiaryStore:
    """日记存储管理: 同一天的多次记录追加到同一个文件"""

    def __init__(self, data_path: str):
        self.data_path = Path(data_path)
        self.diaries_dir = self.data_path / "diaries"
        self.diaries_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _get_diary_file(self, date_str: str) -> Path:
        return self.diaries_dir / f"{validate_date(date_str)}.md"

    @staticmethod
    def _render_header(date_str: str) -> str:
        dt = datetime.strptime(date_str, '%Y-%m-%d')
        return f"# {dt.year}年{dt.month}月{dt.day}日 星期{WEEKDAYS[dt.weekday()]}\n\n"

    @staticmethod
    def _render_entry(entry_id: str, time_str: str, summary: str, tags: List[str], entities: dict) -> str:
        tags = [t if t.startswith('#') else f'#{t}' for t in tags if t.strip()]
        meta = {"id": entry_id, "time": time_str, "tags": tags, "entities": entities}
        summary = " ".join(summary.split())  # 总结保持单行,保证可解析
        return (
            f"### 🕐 {time_str}\n\n"
            f"<!-- META: {json.dumps(meta, ensure_ascii=False)} -->\n\n"
            f"**总结**: {summary}\n\n"
            f"**标签**: {' '.join(tags) or '无'}\n\n"
            f"**关键信息**:\n"
            f"- 人物: {', '.join(entities.get('people') or []) or '无'}\n"
            f"- 地点: {', '.join(entities.get('places') or []) or '无'}\n"
            f"- 事项: {', '.join(entities.get('events') or []) or '无'}\n"
            f"- 情绪: {entities.get('emotion') or '平静'}\n\n"
            f"---\n\n"
        )

    def _read_sections(self, date_str: str) -> tuple[str, List[str]]:
        f = self._get_diary_file(date_str)
        if not f.exists():
            return "", []
        parts = SECTION_SPLIT.split(f.read_text(encoding='utf-8'))
        return parts[0], parts[1:]

    def _write_sections(self, date_str: str, header: str, sections: List[str]) -> None:
        f = self._get_diary_file(date_str)
        if not sections:
            f.unlink(missing_ok=True)  # 当天最后一条被删除时移除文件,日历不再高亮
            return
        tmp = f.with_suffix('.md.tmp')
        tmp.write_text((header or self._render_header(date_str)) + "".join(sections), encoding='utf-8')
        tmp.replace(f)

    @staticmethod
    def _parse_section(section: str, date_str: str) -> Optional[DiaryEntryMetadata]:
        meta_m = META_RE.search(section)
        time_m = TIME_RE.search(section)
        sum_m = SUMMARY_RE.search(section)
        if not (meta_m and time_m and sum_m):
            return None
        try:
            meta = json.loads(meta_m.group(1))
            return DiaryEntryMetadata(
                id=meta['id'],
                date=date_str,
                time=time_m.group(1),
                summary=" ".join(sum_m.group(1).split()),
                tags=meta.get('tags') or [],
                entities=meta.get('entities') or {},
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None

    def save_entry(self, date_str: str, time_str: str, summary: str, tags: List[str], entities: dict) -> str:
        """追加一条记录到当天文件,返回entry_id"""
        entry_id = uuid.uuid4().hex[:12]
        with self._lock:
            f = self._get_diary_file(date_str)
            if not f.exists():
                f.write_text(self._render_header(date_str), encoding='utf-8')
            with open(f, 'a', encoding='utf-8') as fh:
                fh.write(self._render_entry(entry_id, time_str, summary, tags, entities))
        return entry_id

    def get_entries_by_date(self, date_str: str) -> List[DiaryEntryMetadata]:
        _, sections = self._read_sections(date_str)
        entries = [self._parse_section(s, date_str) for s in sections]
        return [e for e in entries if e]

    def update_entry(self, date_str: str, entry_id: str, summary: str, tags: List[str]) -> Optional[DiaryEntryMetadata]:
        """修改某条记录的总结和标签"""
        with self._lock:
            header, sections = self._read_sections(date_str)
            for i, s in enumerate(sections):
                e = self._parse_section(s, date_str)
                if e and e.id == entry_id:
                    sections[i] = self._render_entry(entry_id, e.time, summary, tags, e.entities)
                    self._write_sections(date_str, header, sections)
                    return self._parse_section(sections[i], date_str)
        return None

    def delete_entry(self, date_str: str, entry_id: str) -> bool:
        with self._lock:
            header, sections = self._read_sections(date_str)
            kept = [s for s in sections if not ((e := self._parse_section(s, date_str)) and e.id == entry_id)]
            if len(kept) == len(sections):
                return False
            self._write_sections(date_str, header, kept)
            return True

    def get_calendar_data(self, year: int, month: int) -> dict:
        """返回 {day: 条目数} """
        result = {}
        for f in self.diaries_dir.glob(f"{year:04d}-{month:02d}-??.md"):
            try:
                n = len(self.get_entries_by_date(f.stem))
            except ValueError:
                continue
            if n:
                result[int(f.stem[-2:])] = n
        return result

    def _all_dates(self) -> List[str]:
        return sorted(f.stem for f in self.diaries_dir.glob("*.md") if DATE_RE.match(f.stem))

    def search_entries(
        self,
        keyword: Optional[str] = None,
        tags: Optional[List[str]] = None,
        date_range: Optional[tuple] = None,
    ) -> List[DiaryEntryMetadata]:
        """关键词/标签/日期范围搜索,按时间倒序"""
        if date_range:
            start = datetime.strptime(validate_date(date_range[0]), '%Y-%m-%d').date()
            end = datetime.strptime(validate_date(date_range[1]), '%Y-%m-%d').date()
            dates = []
            while start <= end:
                dates.append(start.strftime('%Y-%m-%d'))
                start += timedelta(days=1)
        else:
            dates = self._all_dates()

        kw = (keyword or "").strip().lower()
        tag_set = {t if t.startswith('#') else f'#{t}' for t in (tags or []) if t}
        results = []
        for d in dates:
            for e in self.get_entries_by_date(d):
                if kw:
                    haystack = " ".join([
                        e.summary, " ".join(e.tags),
                        *[" ".join(v) if isinstance(v, list) else str(v) for v in e.entities.values()],
                    ]).lower()
                    if kw not in haystack:
                        continue
                if tag_set and not tag_set.intersection(e.tags):
                    continue
                results.append(e)
        results.sort(key=lambda e: (e.date, e.time), reverse=True)
        return results

    def all_tags(self) -> List[tuple]:
        """所有标签及出现次数"""
        counts = {}
        for d in self._all_dates():
            for e in self.get_entries_by_date(d):
                for t in e.tags:
                    counts[t] = counts.get(t, 0) + 1
        return sorted(counts.items(), key=lambda x: -x[1])

    def get_all_entries_for_indexing(self) -> List[tuple]:
        """[(entry_id, date, time, summary, tags, entities), ...]"""
        out = []
        for d in self._all_dates():
            for e in self.get_entries_by_date(d):
                out.append((e.id, e.date, e.time, e.summary, e.tags, e.entities))
        return out
