from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.config import Settings


class ConfigTests(unittest.TestCase):
    """验证运行预算环境变量的解析与校验。"""

    def test_model_timeout_and_retry_settings(self):
        """模型超时和重试次数应从环境变量独立读取。"""
        with patch.dict(
            os.environ,
            {"MODEL_TIMEOUT_SECONDS": "75", "MODEL_MAX_RETRIES": "2", "TOOL_TIMEOUT_SECONDS": "20"},
        ):
            settings = Settings.from_env()

        self.assertEqual(settings.model_timeout_seconds, 75)
        self.assertEqual(settings.model_max_retries, 2)
        self.assertEqual(settings.tool_timeout_seconds, 20)

    def test_negative_model_retries_are_rejected(self):
        """重试次数不能为负数。"""
        with patch.dict(os.environ, {"MODEL_MAX_RETRIES": "-1"}):
            with self.assertRaisesRegex(ValueError, "MODEL_MAX_RETRIES"):
                Settings.from_env()

    def test_embedding_configuration_controls_knowledge_base_readiness(self):
        """知识库只有在独立 Embedding 配置完整时才应就绪。"""
        with patch.dict(
            os.environ,
            {
                "EMBEDDING_MODEL": "text-embedding",
                "EMBEDDING_BASE_URL": "https://provider.example/v1",
                "EMBEDDING_API_KEY": "secret",
                "CHUNK_SIZE": "1000",
                "CHUNK_OVERLAP": "100",
            },
        ):
            settings = Settings.from_env()

        self.assertTrue(settings.knowledge_base_ready)
        self.assertEqual(settings.max_upload_bytes, settings.max_upload_mb * 1024 * 1024)

    def test_invalid_chunk_overlap_disables_knowledge_base(self):
        """重叠长度不小于 Chunk 时不得启动知识库。"""
        with patch.dict(
            os.environ,
            {
                "EMBEDDING_MODEL": "text-embedding",
                "EMBEDDING_BASE_URL": "https://provider.example/v1",
                "EMBEDDING_API_KEY": "secret",
                "CHUNK_SIZE": "500",
                "CHUNK_OVERLAP": "500",
            },
        ):
            settings = Settings.from_env()

        self.assertFalse(settings.knowledge_base_ready)


if __name__ == "__main__":
    unittest.main()
