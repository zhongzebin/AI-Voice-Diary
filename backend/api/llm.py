"""LLM API封装 - DeepSeek (OpenAI兼容接口)"""
import json
from datetime import date
from typing import AsyncIterator, List, Optional

from openai import AsyncOpenAI

EMOTIONS = ["开心", "平静", "难过", "焦虑", "兴奋", "疲惫", "生气"]

SUMMARY_PROMPT = """你是用户的语音日记助手。下面是用户口述日记的语音识别文本(可能有错别字、口语重复),原文不会被保存,所以你的总结是这条日记唯一留存的内容。

【识别文本】
{text}

请返回JSON:
{{
  "title": "8-15字的小标题",
  "summary": "第一人称、通顺书面语的完整总结。保留所有具体事实: 人名、地点、时间、数字、做了什么、结果、计划、感受。不添加原文没有的内容。原文很短时可以很短",
  "tags": ["工作"],
  "entities": {{
    "people": ["提到的人"],
    "places": ["提到的地点"],
    "events": ["关键事项, 每项不超过12字"],
    "emotion": "{emotions} 中选一个"
  }}
}}
tags 1-5个, 不带#号。没有的字段返回空数组。"""

ANSWER_SYSTEM = """你是用户的私人日记助手。用户会提问,你只能依据【日记片段】回答。
规则:
1. 只用片段里的信息,不编造;片段不足以回答就直说"日记里没有找到相关记录",可以说明找到了哪些相近内容
2. 每个事实后用[序号]标注来源,如"你和张经理开了会[2]";序号必须是片段里存在的
3. 涉及时间时写出具体日期
4. 用第二人称"你"称呼用户,简洁自然
5. 输出纯文本,不要使用Markdown符号(如**、#)"""

INTENT_PROMPT = """今天是 {today}(星期{weekday})。分析用户对自己日记的提问,返回JSON:
{{"date_range": {{"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}} 或 null, "query": "用于语义检索的改写问题"}}

规则:
- 问题含时间限定(今天/昨天/上周/这个月/3月/最近三天/去年国庆等)时换算成具体日期范围; "最近"不带数字按近7天; 没有时间限定返回 null
- "上周"指上一个自然周(周一到周日)
- query: 去掉时间词, 结合上一轮对话补全指代(如"那他呢"), 保留人物/地点/事件等核心词
{history}
【问题】{question}"""


class LLMProvider:
    """DeepSeek 封装: 总结 / 意图解析 / 流式问答"""

    def __init__(self, api_key: str, model: str = "deepseek-chat"):
        self.client = AsyncOpenAI(api_key=api_key, base_url="https://api.deepseek.com", timeout=60.0, max_retries=2)
        self.model = model

    async def _json_chat(self, prompt: str, temperature: float) -> dict:
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=temperature,
        )
        return json.loads(resp.choices[0].message.content or "{}")

    async def ping(self) -> None:
        """连通性测试(API Key是否有效)"""
        await self.client.chat.completions.create(
            model=self.model, messages=[{"role": "user", "content": "ping"}], max_tokens=1,
        )

    async def summarize_text(self, text: str) -> dict:
        """
        总结语音识别文本

        Returns:
            {"title", "summary", "tags": ["#工作"], "entities": {people, places, events, emotion}}
        """
        try:
            r = await self._json_chat(SUMMARY_PROMPT.format(text=text, emotions="/".join(EMOTIONS)), 0.3)
        except Exception as e:
            raise RuntimeError(f"LLM总结失败: {e}") from e

        summary = str(r.get("summary") or "").strip()
        if not summary:
            raise RuntimeError("LLM总结失败: 返回内容缺少summary")
        title = str(r.get("title") or "").strip()

        def str_list(v, limit=10):
            return [str(x).strip() for x in (v if isinstance(v, list) else []) if str(x).strip()][:limit]

        ent = r.get("entities") if isinstance(r.get("entities"), dict) else {}
        emotion = str(ent.get("emotion") or "平静").strip()
        tags = []
        for t in str_list(r.get("tags"), 5):
            t = "#" + t.lstrip("#").replace(" ", "")
            if len(t) > 1 and t not in tags:
                tags.append(t)
        return {
            "summary": f"【{title}】{summary}" if title else summary,
            "tags": tags,
            "entities": {
                "people": str_list(ent.get("people")),
                "places": str_list(ent.get("places")),
                "events": str_list(ent.get("events")),
                "emotion": emotion if emotion in EMOTIONS else "平静",
            },
        }

    async def extract_query_intent(self, question: str, history: Optional[List[dict]] = None,
                                   today: Optional[date] = None) -> dict:
        """解析提问中的时间范围并改写检索query, 失败时退化为原问题"""
        today = today or date.today()
        hist = ""
        if history:
            last = [m for m in history if m.get("role") in ("user", "assistant")][-2:]
            if last:
                hist = "\n【上一轮对话】\n" + "\n".join(
                    f"{'用户' if m['role'] == 'user' else '助手'}: {str(m.get('content', ''))[:300]}" for m in last)
        prompt = INTENT_PROMPT.format(
            today=today.isoformat(), weekday="一二三四五六日"[today.weekday()], history=hist, question=question)
        try:
            r = await self._json_chat(prompt, 0.0)
            return {"date_range": r.get("date_range"), "query": str(r.get("query") or question)}
        except Exception:
            return {"date_range": None, "query": question}

    async def answer_question(self, question: str, sources: List[dict],
                              history: Optional[List[dict]] = None) -> AsyncIterator[str]:
        """
        基于检索片段流式回答

        Args:
            sources: [{"index": 1, "date", "time", "summary", "tags"}, ...] index 即引用序号
            history: [{"role": "user"|"assistant", "content": str}, ...]
        """
        context = "\n\n".join(
            f"[{s['index']}] {s['date']} {s['time']} {' '.join(s.get('tags') or [])}\n{s['summary']}" for s in sources)
        messages = [{"role": "system", "content": ANSWER_SYSTEM}]
        for m in (history or [])[-6:]:
            if m.get("role") in ("user", "assistant") and m.get("content"):
                messages.append({"role": m["role"], "content": str(m["content"])[:2000]})
        messages.append({"role": "user", "content": f"【日记片段】\n{context}\n\n【问题】\n{question}"})

        stream = await self.client.chat.completions.create(
            model=self.model, messages=messages, temperature=0.3, stream=True)
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
