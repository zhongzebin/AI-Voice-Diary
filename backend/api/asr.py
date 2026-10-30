"""语音识别API封装 - DashScope Paraformer"""
import base64
import json
from typing import Optional
from contextlib import closing
from httpx_sse import EventSource
from dashscope import Client
from dashscope.errors import DashScopeError


class ASRProvider:
    """语音识别提供商"""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.model = "qwen-audio-3.1-asr-flash"

    def transcribe(self, audio_data: bytes, sample_rate: int = 16000) -> str:
        """
        将音频转换为文字

        Args:
            audio_data: WAV格式音频数据(16-bit PCM)
            sample_rate: 采样率(默认16000Hz)

        Returns:
            识别出的文字

        Raises:
            Exception: 识别失败
        """
        # 将音频编码为base64
        audio_b64 = base64.b64encode(audio_data).decode("ascii")
        audio_url = f"data:audio/wav;base64,{audio_b64}"

        # 构建请求体
        body = {
            "model": self.model,
            "input": {
                "messages": [{
                    "role": "user",
                    "content": [{
                        "type": "input_audio",
                        "input_audio": {"data": audio_url}
                    }],
                }]
            },
            "parameters": {
                "format": "wav",
                "sample_rate": str(sample_rate)
            },
        }

        try:
            with Client(api_key=self.api_key, timeout=120.0, max_retries=0) as client:
                # 发送SSE流式请求
                path = "/services/aigc/multimodal-generation/generation"
                with closing(client.transport.open_stream(
                    "POST",
                    path,
                    json=body,
                    headers={"X-DashScope-SSE": "enable"},
                    timeout=120.0,
                    sdk_module="audio",
                )) as response:
                    return self._receive_transcript(response)

        except DashScopeError as e:
            raise Exception(f"语音识别失败: {e.message}") from e
        except Exception as e:
            raise Exception(f"语音识别出错: {str(e)}") from e

    def _receive_transcript(self, response) -> str:
        """从SSE流中接收转写结果"""
        text = ""
        count = 0

        for event in EventSource(response).iter_sse():
            if not event.data:
                continue

            body = json.loads(event.data)
            if not isinstance(body, dict):
                raise ValueError("ASR SSE事件必须为JSON对象")

            # 检查错误
            if event.event == "error" or body.get("code"):
                code = body.get("code", "UNKNOWN")
                message = body.get("message", "语音识别失败")
                raise Exception(f"ASR错误 [{code}]: {message}")

            # 提取文本
            output = body.get("output")
            if not isinstance(output, dict) or "text" not in output:
                continue

            text = output["text"]
            if not isinstance(text, str):
                raise ValueError("ASR output.text必须为字符串")

            count += 1

        if not count:
            raise RuntimeError("HTTP流结束,但没有收到识别结果")

        return text.strip()


def test_asr(api_key: str, audio_path: str) -> None:
    """测试ASR功能"""
    provider = ASRProvider(api_key)

    with open(audio_path, "rb") as f:
        audio_data = f.read()

    text = provider.transcribe(audio_data)
    print(f"识别结果: {text}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 3:
        print("用法: python asr.py <API_KEY> <WAV_FILE>")
        sys.exit(1)

    test_asr(sys.argv[1], sys.argv[2])
