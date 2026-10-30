"""FastAPI 应用: 路由 + 服务装配"""
import io
import os
import sys
import wave
import asyncio
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import config as cfg
from . import platform_utils as plat
from .api.asr import ASRProvider
from .api.embedding import EmbeddingProvider
from .api.llm import LLMProvider
from .api.rag import VectorIndex
from .models import ConfigSetup, ConfigTest, EntryUpdate, QARequest
from .services.diary_service import DiaryStore, validate_date
from .services.qa_service import QAService

log = logging.getLogger(__name__)

MAX_AUDIO_BYTES = 20 * 1024 * 1024  # 16kHz/16bit 单声道约 10 分钟
MIN_SECONDS, MAX_SECONDS = 0.5, 300
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
CLIENT_HEADER = "X-Diary-Client"


def resource_dir() -> Path:
    """静态资源目录(兼容PyInstaller打包)"""
    return plat.resource_dir()


class Services:
    """按当前配置懒加载的服务集合; 配置变化后 reset() 重建"""

    def __init__(self):
        self._lock = threading.Lock()
        self._built: Optional[dict] = None
        self.factory = None  # 测试时注入: factory(data_path, dashscope_key, deepseek_key) -> dict

    def reset(self):
        with self._lock:
            self._built = None

    def get(self) -> dict:
        with self._lock:
            if self._built is None:
                data_path = cfg.get_data_path()
                ds_key, dk_key = cfg.get_api_keys()
                if not (data_path and ds_key and dk_key):
                    raise HTTPException(409, "尚未完成配置")
                self._built = (self.factory or default_factory)(data_path, ds_key, dk_key)
            return self._built


def default_factory(data_path: str, dashscope_key: str, deepseek_key: str) -> dict:
    llm = LLMProvider(deepseek_key)
    store = DiaryStore(data_path)
    index = VectorIndex(data_path, EmbeddingProvider(dashscope_key))
    return {"asr": ASRProvider(dashscope_key), "llm": llm, "store": store,
            "index": index, "qa": QAService(llm, index)}


def read_wav(data: bytes) -> tuple[int, float]:
    """校验WAV: 单声道16bit PCM; 返回 (采样率, 时长秒)"""
    try:
        with wave.open(io.BytesIO(data)) as w:
            ch, width, rate, frames = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
    except (wave.Error, EOFError) as e:
        raise HTTPException(400, f"音频格式无效: {e}")
    if ch != 1 or width != 2:
        raise HTTPException(400, "音频需为单声道 16bit PCM WAV")
    if rate not in (8000, 16000):
        raise HTTPException(400, "音频采样率需为 16000Hz")
    return rate, frames / float(rate)


def create_app() -> FastAPI:
    app = FastAPI(title="AI语音日记", docs_url=None, redoc_url=None, openapi_url=None)
    services = Services()
    app.state.services = services
    sync_lock = asyncio.Lock()

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        # 只服务本机: 拒绝非localhost的Host(防DNS重绑定), 写操作要求自定义头(跨站请求无法携带, 防CSRF)
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in ALLOWED_HOSTS:
            return JSONResponse({"detail": "forbidden host"}, status_code=403)
        if request.url.path.startswith("/api/") and request.method != "GET" \
                and request.headers.get(CLIENT_HEADER) != "1":
            return JSONResponse({"detail": "missing client header"}, status_code=403)
        return await call_next(request)

    async def sync_index(svc: dict) -> dict:
        async with sync_lock:
            entries = await asyncio.to_thread(svc["store"].get_all_entries_for_indexing)
            return await svc["index"].sync(entries)

    # ---------------- 配置 ----------------
    @app.get("/api/health")
    async def health():
        return {"app": "ai-voice-diary", "ok": True}

    @app.get("/api/config/status")
    async def config_status():
        ds, dk = cfg.get_api_keys()
        data_path = cfg.get_data_path()
        default_path = cfg.get_default_data_path()
        return {"configured": cfg.is_configured() and bool(ds and dk), "data_path": data_path,
                "default_data_path": default_path, "has_dashscope_key": bool(ds), "has_deepseek_key": bool(dk)}

    @app.post("/api/config/test")
    async def config_test(body: ConfigTest):
        ds_saved, dk_saved = cfg.get_api_keys()
        ds, dk = (body.dashscope_key or "").strip() or ds_saved, (body.deepseek_key or "").strip() or dk_saved
        result = {"dashscope_ok": False, "deepseek_ok": False, "dashscope_error": None, "deepseek_error": None}

        async def t_ds():
            if not ds:
                result["dashscope_error"] = "未填写"
                return
            try:
                await EmbeddingProvider(ds).embed_single("连通性测试")
                result["dashscope_ok"] = True
            except Exception as e:
                result["dashscope_error"] = str(e)[:300]

        async def t_dk():
            if not dk:
                result["deepseek_error"] = "未填写"
                return
            try:
                await LLMProvider(dk).ping()
                result["deepseek_ok"] = True
            except Exception as e:
                result["deepseek_error"] = str(e)[:300]

        await asyncio.gather(t_ds(), t_dk())
        return result

    @app.post("/api/config/setup")
    async def config_setup(body: ConfigSetup):
        path = Path(os.path.expandvars(body.data_path.strip())).expanduser()
        if not path.is_absolute():
            raise HTTPException(400, "请填写完整的绝对路径")
        try:
            (path / "diaries").mkdir(parents=True, exist_ok=True)
            probe = path / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as e:
            raise HTTPException(400, f"目录不可写: {e}")
        ds_saved, dk_saved = cfg.get_api_keys()
        if not ((body.dashscope_key or "").strip() or ds_saved) or not ((body.deepseek_key or "").strip() or dk_saved):
            raise HTTPException(400, "请填写两个 API Key")
        cfg.update_config(str(path), body.dashscope_key, body.deepseek_key)
        services.reset()
        return {"ok": True, "data_path": str(path)}

    @app.post("/api/config/pick-folder")
    async def pick_folder():
        """弹出系统文件夹选择框(浏览器无法拿到本地绝对路径, 由后端弹窗)"""
        try:
            path = await asyncio.to_thread(plat.pick_folder, "选择日记存储位置")
        except Exception as e:
            raise HTTPException(500, f"无法打开文件夹选择框, 请手动输入路径 ({e})")
        return {"path": path or ""}

    @app.post("/api/open-folder")
    async def open_folder():
        data_path = cfg.get_data_path()
        if not data_path or not Path(data_path).exists():
            raise HTTPException(404, "数据目录不存在")
        try:
            await asyncio.to_thread(plat.open_folder, str(Path(data_path) / "diaries"))
        except Exception as e:
            raise HTTPException(500, f"无法打开文件夹: {e}")
        return {"ok": True}

    # ---------------- 日记 ----------------
    @app.post("/api/diary/record")
    async def record(audio: UploadFile = File(...), date: Optional[str] = Form(None), time: Optional[str] = Form(None)):
        svc = services.get()
        data = await audio.read(MAX_AUDIO_BYTES + 1)
        if len(data) > MAX_AUDIO_BYTES:
            raise HTTPException(413, "录音过长")
        rate, seconds = read_wav(data)
        if seconds < MIN_SECONDS:
            raise HTTPException(400, "录音太短了, 请至少说一句话")
        if seconds > MAX_SECONDS + 5:
            raise HTTPException(400, f"单条录音最长 {MAX_SECONDS // 60} 分钟")

        now = datetime.now()
        date_str = validate_date(date) if date else now.strftime("%Y-%m-%d")
        if time and not (len(time) == 5 and time[2] == ":" and time.replace(":", "").isdigit()):
            raise HTTPException(400, "时间格式应为 HH:MM")
        time_str = time or now.strftime("%H:%M")

        try:
            transcript = await asyncio.to_thread(svc["asr"].transcribe, data, rate)
        except Exception as e:
            raise HTTPException(502, str(e))
        if not transcript.strip():
            raise HTTPException(422, "没有识别到说话内容, 请靠近麦克风再试一次")

        try:
            s = await svc["llm"].summarize_text(transcript)
        except Exception as e:
            raise HTTPException(502, str(e))

        entry_id = await asyncio.to_thread(svc["store"].save_entry, date_str, time_str,
                                           s["summary"], s["tags"], s["entities"])
        indexed, index_error = True, None
        try:
            await sync_index(svc)
        except Exception as e:  # 已保存到文件, 索引失败不影响日记, 下次同步补上
            indexed, index_error = False, str(e)[:300]
            log.warning("索引同步失败: %s", e)

        return {"entry": {"id": entry_id, "date": date_str, "time": time_str, **s},
                "transcript": transcript, "indexed": indexed, "index_error": index_error}

    @app.get("/api/diary/calendar")
    async def calendar(year: int, month: int):
        if not (1 <= month <= 12 and 1970 <= year <= 2100):
            raise HTTPException(400, "年月不合法")
        days = await asyncio.to_thread(services.get()["store"].get_calendar_data, year, month)
        return {"year": year, "month": month, "days": days}

    @app.get("/api/diary/day/{date_str}")
    async def day(date_str: str):
        try:
            entries = await asyncio.to_thread(services.get()["store"].get_entries_by_date, date_str)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"date": date_str, "entries": [e.model_dump() for e in entries]}

    @app.put("/api/diary/{date_str}/{entry_id}")
    async def update(date_str: str, entry_id: str, body: EntryUpdate):
        svc = services.get()
        try:
            e = await asyncio.to_thread(svc["store"].update_entry, date_str, entry_id, body.summary, body.tags)
        except ValueError as ex:
            raise HTTPException(400, str(ex))
        if not e:
            raise HTTPException(404, "记录不存在")
        try:
            await sync_index(svc)
        except Exception as ex:
            log.warning("索引同步失败: %s", ex)
        return e.model_dump()

    @app.delete("/api/diary/{date_str}/{entry_id}")
    async def delete(date_str: str, entry_id: str):
        svc = services.get()
        try:
            ok = await asyncio.to_thread(svc["store"].delete_entry, date_str, entry_id)
        except ValueError as ex:
            raise HTTPException(400, str(ex))
        if not ok:
            raise HTTPException(404, "记录不存在")
        try:
            await sync_index(svc)
        except Exception as ex:
            log.warning("索引同步失败: %s", ex)
        return {"ok": True}

    @app.get("/api/diary/search")
    async def search(q: str = "", tag: str = "", start: str = "", end: str = ""):
        store = services.get()["store"]
        date_range = None
        if start or end:
            date_range = (start or "1970-01-01", end or datetime.now().strftime("%Y-%m-%d"))
        try:
            results = await asyncio.to_thread(store.search_entries, q or None, [tag] if tag else None, date_range)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"total": len(results), "entries": [e.model_dump() for e in results[:200]]}

    @app.get("/api/diary/tags")
    async def tags():
        items = await asyncio.to_thread(services.get()["store"].all_tags)
        return {"tags": [{"tag": t, "count": c} for t, c in items]}

    # ---------------- 问答 ----------------
    @app.post("/api/qa/ask")
    async def ask(body: QARequest):
        svc = services.get()
        try:
            await sync_index(svc)  # 用户手动改过md文件也能被检索到
        except Exception as e:
            log.warning("索引同步失败: %s", e)
        history = [m for m in body.history if isinstance(m, dict)][-6:]
        return StreamingResponse(svc["qa"].ask_stream(body.question.strip(), history),
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/index/status")
    async def index_status():
        return {"count": services.get()["index"].count()}

    @app.post("/api/index/rebuild")
    async def index_rebuild():
        try:
            return await sync_index(services.get())
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"索引同步失败: {e}")

    # ---------------- 静态页面 ----------------
    static_dir = resource_dir() / "static"

    @app.get("/")
    async def index_page():
        return FileResponse(static_dir / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    return app
