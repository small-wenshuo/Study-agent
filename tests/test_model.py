from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.config import Settings
from app.providers.model import OpenAICompatibleModel


class OpenAICompatibleModelTests(unittest.TestCase):
    """验证聊天模型适配器只发送广泛兼容的参数。"""

    @patch("app.providers.model.ChatOpenAI")
    def test_client_does_not_force_unsupported_temperature(self, client_class):
        """Kimi K3 等模型不支持 temperature，适配层不得强制传入。"""
        with patch.dict(
            os.environ,
            {
                "MODEL_NAME": "compatible-model",
                "MODEL_BASE_URL": "https://provider.example/v1",
                "MODEL_API_KEY": "test-key",
            },
        ):
            OpenAICompatibleModel(Settings.from_env())

        self.assertNotIn("temperature", client_class.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
