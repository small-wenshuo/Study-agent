from __future__ import annotations

from langchain_openai import OpenAIEmbeddings

from app.config import Settings


class OpenAICompatibleEmbeddings:
    """把 OpenAI-compatible Embeddings 接口适配给知识库服务。"""

    def __init__(self, settings: Settings) -> None:
        """使用独立 Embedding 配置创建客户端。"""
        self._client = OpenAIEmbeddings(
            model=settings.embedding_model,
            api_key=settings.embedding_api_key,
            base_url=settings.embedding_base_url,
            timeout=settings.tool_timeout_seconds,
            max_retries=1,
        )

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """批量向量化 Chunk。"""
        return await self._client.aembed_documents(texts)

    async def embed_query(self, text: str) -> list[float]:
        """向量化用户检索问题。"""
        return await self._client.aembed_query(text)
