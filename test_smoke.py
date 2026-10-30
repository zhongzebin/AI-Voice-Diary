"""快速烟雾测试: 验证 API 端点和服务流程"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient
from backend.server import create_app


class FakeASR:
    def __init__(self, key): pass
    def transcribe(self, data, rate): return "今天天气很好"


class FakeLLM:
    def __init__(self, key): pass
    async def summarize_text(self, text):
        return {"summary": "测试总结", "tags": ["测试"], "entities": {"emotion": "开心", "people": [], "places": [], "events": []}}
    async def extract_query_intent(self, question, history, today):
        return {"date_range": None, "query": question}
    async def answer_question(self, question, sources, history):
        yield "这是测试回答"
    async def ping(self): pass


class FakeEmbed:
    def __init__(self, key): pass
    dimension = 1024
    async def embed_texts(self, texts): return [[0.1] * 1024] * len(texts)
    async def embed_single(self, text): return [0.1] * 1024


def fake_factory(data_path, ds_key, dk_key):
    from backend.services.diary_service import DiaryStore
    from backend.api.rag import VectorIndex
    from backend.services.qa_service import QAService
    llm = FakeLLM(dk_key)
    store = DiaryStore(data_path)
    index = VectorIndex(data_path, FakeEmbed(ds_key))
    return {"asr": FakeASR(ds_key), "llm": llm, "store": store, "index": index, "qa": QAService(llm, index)}


def test_smoke():
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_dir = Path(tmpdir) / "config"
        cfg_dir.mkdir()
        data_dir = Path(tmpdir) / "data"
        data_dir.mkdir()

        os.environ["AI_DIARY_CONFIG_DIR"] = str(cfg_dir)

        # 强制重新导入 config 模块让环境变量生效
        import importlib
        from backend import config as cfg_module
        importlib.reload(cfg_module)

        app = create_app()
        app.state.services.factory = fake_factory
        client = TestClient(app, base_url="http://127.0.0.1", headers={"X-Diary-Client": "1"})

        # 0. 安全中间件: 非本机Host、缺少客户端头的写请求都应拒绝
        assert TestClient(app).get("/api/health").status_code == 403
        assert TestClient(app, base_url="http://127.0.0.1").post("/api/index/rebuild").status_code == 403
        print("✓ security middleware")

        # 1. 健康检查
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["ok"] is True
        print("✓ health endpoint")

        # 2. 配置状态(未配置)
        r = client.get("/api/config/status")
        assert r.status_code == 200
        assert r.json()["configured"] is False
        print("✓ config status (unconfigured)")

        # 3. 保存配置
        r = client.post("/api/config/setup", json={
            "data_path": str(data_dir),
            "dashscope_key": "sk-test-ds",
            "deepseek_key": "sk-test-dk",
        })
        assert r.status_code == 200
        print("✓ config setup")

        # 4. 配置状态(已配置)
        r = client.get("/api/config/status")
        assert r.status_code == 200
        assert r.json()["configured"] is True
        print("✓ config status (configured)")

        # 5. 录音(模拟WAV)
        import wave, io
        wav = io.BytesIO()
        with wave.open(wav, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x00\x01" * 16000)  # 1秒静音
        wav.seek(0)

        r = client.post("/api/diary/record", files={"audio": ("test.wav", wav, "audio/wav")})
        assert r.status_code == 200
        entry = r.json()["entry"]
        assert entry["date"]
        assert entry["time"]
        print(f"✓ record entry {entry['id']}")

        # 6. 日历查询
        y, m, _ = entry["date"].split("-")
        r = client.get(f"/api/diary/calendar?year={int(y)}&month={int(m)}")
        assert r.status_code == 200
        days = r.json()["days"]
        assert any(days.values())  # 至少有一天有记录
        print("✓ calendar")

        # 7. 日期查询
        date = entry["date"]
        r = client.get(f"/api/diary/day/{date}")
        assert r.status_code == 200
        entries = r.json()["entries"]
        assert len(entries) >= 1
        print(f"✓ day entries ({len(entries)})")

        # 8. 标签列表
        r = client.get("/api/diary/tags")
        assert r.status_code == 200
        tags = r.json()["tags"]
        assert len(tags) >= 1
        print(f"✓ tags ({len(tags)})")

        # 9. 搜索
        r = client.get("/api/diary/search?q=测试")
        assert r.status_code == 200
        print("✓ search")

        # 10. 编辑
        entry_id = entries[0]["id"]
        r = client.put(f"/api/diary/{date}/{entry_id}", json={"summary": "修改后的总结", "tags": ["新标签"]})
        assert r.status_code == 200
        updated = r.json()
        assert updated["summary"] == "修改后的总结"
        assert updated["tags"] == ["#新标签"], updated["tags"]
        print("✓ update entry")

        # 11. 问答(SSE流)
        r = client.post("/api/qa/ask", json={"question": "测试问题", "history": []})
        assert r.status_code == 200
        assert "text/event-stream" in r.headers["content-type"]
        assert "event: sources" in r.text and "event: done" in r.text, r.text
        print("✓ qa ask (streaming)")

        # 12. 索引状态
        r = client.get("/api/index/status")
        assert r.status_code == 200
        assert r.json()["count"] >= 0
        print("✓ index status")

        # 13. 删除
        r = client.delete(f"/api/diary/{date}/{entry_id}")
        assert r.status_code == 200
        print("✓ delete entry")

        # 14. 静态页面
        r = client.get("/")
        assert r.status_code == 200
        assert "AI语音日记" in r.text
        print("✓ index page")

        print("\n✅ All smoke tests passed")


if __name__ == "__main__":
    test_smoke()
