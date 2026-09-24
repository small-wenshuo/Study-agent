from __future__ import annotations

import unittest
from unittest.mock import patch

from app.config import Settings
from app.providers.embeddings import OpenAICompatibleEmbeddings


class OpenAICompatibleEmbeddingsTests(unittest.TestCase):
    """验证 OpenAI-compatible Embedding 客户端的兼容参数。"""

    @patch("app.providers.embeddings.OpenAIEmbeddings")
    def test_client_sends_original_strings_without_local_tokenization(self, client_class):
        """DashScope 兼容接口应收到字符串，而不是 LangChain 生成的 Token 数组。"""
        settings = Settings.from_env()

        OpenAICompatibleEmbeddings(settings)

        self.assertFalse(client_class.call_args.kwargs["check_embedding_ctx_length"])


if __name__ == "__main__":
    unittest.main()
