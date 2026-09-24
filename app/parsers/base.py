from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class ParseError(ValueError):
    """表示文件格式、内容或解析结果不满足入库要求。"""


@dataclass(frozen=True, slots=True)
class Document:
    """解析器统一输出的可检索正文及来源元数据。"""

    content: str
    metadata: dict[str, Any]


class DocumentParser(Protocol):
    """单一文件格式解析器需要实现的最小接口。"""

    extensions: frozenset[str]

    def parse(self, filename: str, data: bytes) -> list[Document]:
        """把原始文件字节转换为统一 Document。"""
        ...
