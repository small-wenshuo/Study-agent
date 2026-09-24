from __future__ import annotations

from langchain_openai import OpenAIEmbeddings

from app.config import Settings


class OpenAICompatibleEmbeddings:
    """把 OpenAI-compatible Embeddings 接口适配给知识库服务。"""

    _MAX_BATCH_SIZE = 25

    def __init__(self, settings: Settings) -> None:
        """使用独立 Embedding 配置创建客户端。"""
        self._client = OpenAIEmbeddings(
            model=settings.embedding_model,
            api_key=settings.embedding_api_key,
            base_url=settings.embedding_base_url,
            timeout=settings.tool_timeout_seconds,
            max_retries=1,
            # DashScope 的兼容接口只接受字符串，不能接收 LangChain 本地分词后的 Token 数组。
            check_embedding_ctx_length=False,
        )

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """按供应商上限拆分 Chunk，并保持向量与原文本顺序一致。"""
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._MAX_BATCH_SIZE):
            batch = texts[start : start + self._MAX_BATCH_SIZE]
            vectors.extend(await self._client.aembed_documents(batch))
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        """向量化用户检索问题。"""
        return await self._client.aembed_query(text)
