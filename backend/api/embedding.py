"""Embedding API封装 - DashScope text-embedding-v3 (OpenAI兼容接口)"""
from typing import List
from openai import AsyncOpenAI


class EmbeddingProvider:
    """Embedding提供商 - DashScope (通过OpenAI兼容接口)"""

    def __init__(self, api_key: str):
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1"
        )
        self.model = "text-embedding-v3"
        self.dimension = 1024

    async def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """
        批量生成文本embeddings

        Args:
            texts: 文本列表(最多10条,避免超时)

        Returns:
            embedding向量列表
        """
        if not texts:
            return []

        if len(texts) > 10:
            raise ValueError("单次最多支持10条文本")

        try:
            response = await self.client.embeddings.create(
                model=self.model,
                input=texts,
                encoding_format="float"
            )

            embeddings = [item.embedding for item in response.data]

            # 验证维度
            for emb in embeddings:
                if len(emb) != self.dimension:
                    raise ValueError(f"Embedding维度不匹配: 期望{self.dimension}, 实际{len(emb)}")

            return embeddings

        except Exception as e:
            raise Exception(f"Embedding生成失败: {str(e)}") from e

    async def embed_single(self, text: str) -> List[float]:
        """生成单个文本的embedding"""
        embeddings = await self.embed_texts([text])
        return embeddings[0]


async def test_embedding(api_key: str):
    """测试Embedding功能"""
    provider = EmbeddingProvider(api_key)

    texts = [
        "今天天气很好",
        "下午和团队开会",
        "晚上看了一部电影"
    ]

    print(f"测试embedding生成 (模型: {provider.model}, 维度: {provider.dimension})...")
    embeddings = await provider.embed_texts(texts)

    for i, (text, emb) in enumerate(zip(texts, embeddings)):
        print(f"文本{i+1}: {text}")
        print(f"  向量维度: {len(emb)}")
        print(f"  前5维: {emb[:5]}")


if __name__ == "__main__":
    import sys
    import asyncio

    if len(sys.argv) != 2:
        print("用法: python embedding.py <DASHSCOPE_API_KEY>")
        sys.exit(1)

    asyncio.run(test_embedding(sys.argv[1]))
