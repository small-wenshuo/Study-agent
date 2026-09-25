from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from app.config import Settings
from app.providers.embeddings import OpenAICompatibleEmbeddings


def safe_settings() -> Settings:
    """使用无敏感信息的配置，避免失败断言打印本机密钥。"""
    return replace(
        Settings.from_env(),
        embedding_model="test-embedding",
        embedding_base_url="https://provider.example/v1",
        embedding_api_key="test-key",
    )


class OpenAICompatibleEmbeddingsTests(unittest.IsolatedAsyncioTestCase):
    """验证 OpenAI-compatible Embedding 客户端的兼容参数。"""

    @patch("app.providers.embeddings.OpenAIEmbeddings")
    def test_client_sends_original_strings_without_local_tokenization(self, client_class):
        """DashScope 兼容接口应收到字符串，而不是 LangChain 生成的 Token 数组。"""
        settings = safe_settings()

        OpenAICompatibleEmbeddings(settings)

        self.assertFalse(client_class.call_args.kwargs["check_embedding_ctx_length"])

    @patch("app.providers.embeddings.OpenAIEmbeddings")
    async def test_documents_are_split_into_provider_safe_batches(self, client_class):
        """超过 20 段时应拆批，避免触发 DashScope 的批量上限。"""
        async def embed_batch(texts: list[str]) -> list[list[float]]:
            if len(texts) > 20:
                raise ValueError("batch size is invalid, it should not be larger than 20")
            return [[float(index)] for index, _ in enumerate(texts)]

        client_class.return_value.aembed_documents = AsyncMock(side_effect=embed_batch)
        embeddings = OpenAICompatibleEmbeddings(safe_settings())

        vectors = await embeddings.embed_documents([f"chunk-{index}" for index in range(41)])

        self.assertEqual(len(vectors), 41)
        self.assertEqual(
            [len(call.args[0]) for call in client_class.return_value.aembed_documents.await_args_list],
            [20, 20, 1],
        )


if __name__ == "__main__":
    unittest.main()
