"""问答服务 - RAG流程编排: 意图解析 → 检索 → 生成 → 引用溯源"""
import re
import json
from datetime import date, datetime
from typing import AsyncIterator, List, Optional

from ..api.llm import LLMProvider
from ..api.rag import VectorIndex

TOP_K = 6
MAX_DATE_RANGE_HITS = 30
CITE_RE = re.compile(r'\[(\d+)\]')


def _valid_range(dr) -> Optional[tuple]:
    if not isinstance(dr, dict):
        return None
    try:
        s = datetime.strptime(dr.get("start", ""), "%Y-%m-%d").date()
        e = datetime.strptime(dr.get("end", ""), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    if s > e:
        s, e = e, s
    return s.isoformat(), e.isoformat()


def sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class QAService:
    def __init__(self, llm: LLMProvider, index: VectorIndex):
        self.llm = llm
        self.index = index

    async def retrieve(self, question: str, history: List[dict]) -> tuple[List[dict], Optional[tuple]]:
        intent = await self.llm.extract_query_intent(question, history, today=date.today())
        date_range = _valid_range(intent.get("date_range"))
        query = (intent.get("query") or "").strip() or question

        if date_range:
            # 有明确时间范围: 范围内按相关度排序, 范围内条目少时全部给LLM
            hits = await self.index.search(query, top_k=MAX_DATE_RANGE_HITS, date_range=date_range)
            if len(hits) > TOP_K * 2:
                hits = hits[:TOP_K * 2]
        else:
            hits = await self.index.search(query, top_k=TOP_K)
        hits.sort(key=lambda h: (h["date"], h["time"]))
        return hits, date_range

    async def ask_stream(self, question: str, history: List[dict]) -> AsyncIterator[str]:
        """SSE事件流: sources → delta* → done(含实际被引用的序号) / error"""
        try:
            if self.index.count() == 0:
                yield sse("sources", [])
                yield sse("delta", "知识库里还没有日记哦,先去录一段吧。")
                yield sse("done", {"cited": []})
                return

            hits, date_range = await self.retrieve(question, history)
            sources = [{**h, "index": i + 1} for i, h in enumerate(hits)]
            yield sse("sources", sources)

            if not hits:
                msg = "未找到相关记录。"
                if date_range:
                    msg = f"{date_range[0]} 至 {date_range[1]} 期间没有找到相关日记记录。"
                yield sse("delta", msg)
                yield sse("done", {"cited": []})
                return

            answer = []
            async for chunk in self.llm.answer_question(question, sources, history):
                answer.append(chunk)
                yield sse("delta", chunk)

            text = "".join(answer)
            cited = sorted({int(n) for n in CITE_RE.findall(text) if 1 <= int(n) <= len(sources)})
            yield sse("done", {"cited": cited})
        except Exception as e:
            yield sse("error", str(e))
