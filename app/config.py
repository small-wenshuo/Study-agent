from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=False)


def _integer(name: str, default: int, minimum: int) -> int:
    """读取并校验一个带下限的整数环境变量。"""
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是整数") from exc
    if value < minimum:
        raise ValueError(f"{name} 不能小于 {minimum}")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    """集中保存模型、搜索服务和执行预算配置。"""

    model_provider: str
    model_name: str
    model_base_url: str
    model_api_key: str
    search_provider: str
    tavily_api_key: str
    task_timeout_seconds: int = 180
    tool_timeout_seconds: int = 30
    max_search_rounds: int = 3
    max_tool_calls: int = 12

    @classmethod
    def from_env(cls) -> "Settings":
        """从环境变量构造配置，未设置项使用安全默认值。"""
        return cls(
            model_provider=os.getenv("MODEL_PROVIDER", "openai_compatible"),
            model_name=os.getenv("MODEL_NAME", ""),
            model_base_url=os.getenv("MODEL_BASE_URL", ""),
            model_api_key=os.getenv("MODEL_API_KEY", ""),
            search_provider=os.getenv("SEARCH_PROVIDER", "tavily"),
            tavily_api_key=os.getenv("TAVILY_API_KEY", ""),
            task_timeout_seconds=_integer("TASK_TIMEOUT_SECONDS", 180, 1),
            tool_timeout_seconds=_integer("TOOL_TIMEOUT_SECONDS", 30, 1),
            max_search_rounds=_integer("MAX_SEARCH_ROUNDS", 3, 0),
            max_tool_calls=_integer("MAX_TOOL_CALLS", 12, 1),
        )

    @property
    def missing_model_fields(self) -> list[str]:
        """返回尚未填写、会阻止模型启动的配置项名称。"""
        fields = {
            "MODEL_NAME": self.model_name,
            "MODEL_BASE_URL": self.model_base_url,
            "MODEL_API_KEY": self.model_api_key,
        }
        return [name for name, value in fields.items() if not value or value.startswith("replace-")]

    @property
    def model_ready(self) -> bool:
        """判断 OpenAI-compatible 模型配置是否完整。"""
        return self.model_provider == "openai_compatible" and not self.missing_model_fields

    @property
    def search_ready(self) -> bool:
        """判断 Tavily 搜索服务是否已配置。"""
        return self.search_provider == "tavily" and bool(self.tavily_api_key)
