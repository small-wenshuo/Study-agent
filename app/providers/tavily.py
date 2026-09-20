from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx


class SearchProvider(Protocol):
    async def search(self, query: str, max_results: int = 5) -> list[dict[str, str]]: ...
    async def extract(self, urls: list[str]) -> list[dict[str, Any]]: ...


class ProviderError(RuntimeError):
    pass


def _is_public_ip(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return not (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved)


async def validate_public_url(url: str, timeout: float = 5.0) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        raise ValueError("只允许公开 HTTP(S) 地址")
    hostname = parsed.hostname.lower().rstrip(".")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError("拒绝本机地址")
    try:
        if not _is_public_ip(hostname):
            raise ValueError("拒绝私有网络地址")
        return
    except ValueError as exc:
        if "拒绝" in str(exc):
            raise
    loop = asyncio.get_running_loop()
    try:
        records = await asyncio.wait_for(
            loop.getaddrinfo(hostname, parsed.port or 443, type=socket.SOCK_STREAM), timeout=timeout
        )
    except (OSError, TimeoutError) as exc:
        raise ValueError("域名解析失败") from exc
    if not records or any(not _is_public_ip(item[4][0]) for item in records):
        raise ValueError("地址解析到非公开网络")


class TavilyProvider:
    def __init__(self, api_key: str, timeout: int = 30) -> None:
        self._api_key = api_key
        self._timeout = timeout

    async def _post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        payload = {"api_key": self._api_key, **payload}
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=False) as client:
                    response = await client.post(f"https://api.tavily.com/{endpoint}", json=payload)
                if response.status_code in {401, 403}:
                    raise ProviderError("Tavily 认证失败")
                response.raise_for_status()
                return response.json()
            except ProviderError:
                raise
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                last_error = exc
                retryable = not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code in {429, 500, 502, 503, 504}
                if attempt == 0 and retryable:
                    await asyncio.sleep(0.25)
                    continue
                break
        raise ProviderError(f"Tavily 请求失败：{type(last_error).__name__}") from last_error

    async def search(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
        data = await self._post(
            "search",
            {"query": query, "max_results": max_results, "search_depth": "advanced", "include_raw_content": False},
        )
        results: list[dict[str, str]] = []
        for item in data.get("results", []):
            url = str(item.get("url", ""))
            try:
                await validate_public_url(url)
            except ValueError:
                continue
            results.append({
                "title": str(item.get("title") or url),
                "url": url,
                "snippet": str(item.get("content") or "")[:1200],
            })
        return results

    async def extract(self, urls: list[str]) -> list[dict[str, Any]]:
        for url in urls:
            await validate_public_url(url)
        data = await self._post("extract", {"urls": urls, "extract_depth": "advanced", "include_images": False})
        by_url: dict[str, dict[str, Any]] = {}
        for item in data.get("results", []):
            url = str(item.get("url", ""))
            by_url[url] = {"url": url, "content": str(item.get("raw_content") or item.get("content") or "")}
        failures = {str(item.get("url", "")): str(item.get("error") or "读取失败") for item in data.get("failed_results", [])}
        return [by_url.get(url, {"url": url, "error": failures.get(url, "未返回正文")}) for url in urls]
