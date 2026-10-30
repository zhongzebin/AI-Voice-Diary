"""向量检索 - 基于numpy的本地向量索引(替代ChromaDB,便于PyInstaller打包)"""
import json
import hashlib
import threading
from pathlib import Path
from datetime import datetime, date
from typing import List, Optional

import numpy as np

from .embedding import EmbeddingProvider

BATCH_SIZE = 10


def build_entry_text(entry_date: str, entry_time: str, summary: str, tags: List[str], entities: dict) -> str:
    """把日记条目拼成用于向量化的文本"""
    parts = [f"{entry_date} {entry_time}", summary]
    if tags:
        parts.append("标签: " + " ".join(tags))
    for key, label in (("people", "人物"), ("places", "地点"), ("events", "事项")):
        values = entities.get(key) or []
        if values:
            parts.append(f"{label}: {', '.join(values)}")
    if entities.get("emotion"):
        parts.append(f"情绪: {entities['emotion']}")
    return "\n".join(parts)


class VectorIndex:
    """本地向量索引: vectors.npy(L2归一化矩阵) + meta.json(条目元数据)"""

    def __init__(self, data_path: str, embedder: EmbeddingProvider):
        self.dir = Path(data_path) / "vector_db"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.vec_file = self.dir / "vectors.npy"
        self.meta_file = self.dir / "meta.json"
        self.embedder = embedder
        self._lock = threading.Lock()
        self.meta: List[dict] = []
        self.vectors = np.zeros((0, embedder.dimension), dtype=np.float32)
        self._load()

    def _load(self) -> None:
        if not (self.vec_file.exists() and self.meta_file.exists()):
            return
        try:
            vectors = np.load(self.vec_file)
            meta = json.loads(self.meta_file.read_text(encoding="utf-8"))
            if vectors.ndim == 2 and vectors.shape[0] == len(meta) and vectors.shape[1] == self.embedder.dimension:
                self.vectors, self.meta = vectors.astype(np.float32), meta
        except Exception:
            # 索引损坏时清空,下次sync会全量重建
            self.meta, self.vectors = [], np.zeros((0, self.embedder.dimension), dtype=np.float32)

    def _save(self) -> None:
        tmp_vec = self.dir / "vectors.tmp.npy"
        tmp_meta = self.dir / "meta.tmp.json"
        np.save(tmp_vec, self.vectors)
        tmp_meta.write_text(json.dumps(self.meta, ensure_ascii=False), encoding="utf-8")
        tmp_vec.replace(self.vec_file)
        tmp_meta.replace(self.meta_file)

    async def sync(self, entries: List[tuple]) -> dict:
        """
        与日记文件同步: 新增/修改的条目重新向量化, 已删除的条目移出索引

        Args:
            entries: DiaryStore.get_all_entries_for_indexing() 的返回值

        Returns:
            {"added": n, "removed": n, "total": n}
        """
        wanted = {}
        for entry_id, d, t, summary, tags, entities in entries:
            text = build_entry_text(d, t, summary, tags, entities)
            wanted[entry_id] = {
                "id": entry_id, "date": d, "time": t, "summary": summary,
                "tags": tags, "text": text,
                "hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }

        with self._lock:
            keep = [i for i, m in enumerate(self.meta)
                    if m["id"] in wanted and wanted[m["id"]]["hash"] == m["hash"]]
            kept_ids = {self.meta[i]["id"] for i in keep}
            removed = len(self.meta) - len(keep)

        todo = [m for eid, m in wanted.items() if eid not in kept_ids]
        new_vecs = []
        for i in range(0, len(todo), BATCH_SIZE):
            batch = todo[i:i + BATCH_SIZE]
            new_vecs.extend(await self.embedder.embed_texts([m["text"] for m in batch]))

        with self._lock:
            meta = [self.meta[i] for i in keep] + todo
            vecs = self.vectors[keep] if keep else np.zeros((0, self.embedder.dimension), dtype=np.float32)
            if new_vecs:
                arr = np.asarray(new_vecs, dtype=np.float32)
                arr /= np.linalg.norm(arr, axis=1, keepdims=True) + 1e-12
                vecs = np.vstack([vecs, arr])
            self.meta, self.vectors = meta, vecs
            if todo or removed:
                self._save()
            return {"added": len(todo), "removed": removed, "total": len(meta)}

    async def search(
        self,
        query: str,
        top_k: int = 6,
        min_score: float = 0.3,
        date_range: Optional[tuple] = None,
    ) -> List[dict]:
        """
        语义检索

        Args:
            query: 查询文本
            top_k: 返回条数
            min_score: 余弦相似度下限
            date_range: (start, end) YYYY-MM-DD, 先按日期过滤再排序

        Returns:
            [{"id","date","time","summary","tags","score"}, ...] 按相关度降序
        """
        with self._lock:
            if not self.meta:
                return []
            meta, vectors = list(self.meta), self.vectors

        idx = np.arange(len(meta))
        if date_range:
            start, end = date_range
            idx = np.array([i for i, m in enumerate(meta) if start <= m["date"] <= end], dtype=int)
            if idx.size == 0:
                return []

        q = np.asarray(await self.embedder.embed_single(query), dtype=np.float32)
        q /= np.linalg.norm(q) + 1e-12
        scores = vectors[idx] @ q

        # 时间加权: 近30天的记录分数轻微上浮(最多+5%),不改变明显的相关性排序
        today = date.today()
        boosts = np.ones(idx.size, dtype=np.float32)
        for j, i in enumerate(idx):
            try:
                age = (today - datetime.strptime(meta[i]["date"], "%Y-%m-%d").date()).days
            except ValueError:
                continue
            if 0 <= age <= 30:
                boosts[j] = 1.0 + 0.05 * (1 - age / 30)
        ranked = scores * boosts

        order = np.argsort(-ranked)
        results = []
        for j in order:
            if scores[j] < min_score and not date_range:
                continue
            m = meta[idx[j]]
            results.append({
                "id": m["id"], "date": m["date"], "time": m["time"],
                "summary": m["summary"], "tags": m.get("tags", []),
                "score": round(float(scores[j]), 4),
            })
            if len(results) >= top_k:
                break
        return results

    def count(self) -> int:
        return len(self.meta)
