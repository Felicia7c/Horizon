from __future__ import annotations

import asyncio
from types import SimpleNamespace

import src.ai.enricher as enricher_module
from src.ai.enricher import ContentEnricher


def test_web_search_passes_timeout_to_ddgs(monkeypatch):
    captured = {}

    class FakeDDGS:
        def __init__(self, timeout):
            captured["timeout"] = timeout

        def text(self, query, max_results):
            captured["query"] = query
            captured["max_results"] = max_results
            return [{"title": "Result", "href": "https://example.com", "body": "Body"}]

    monkeypatch.setenv("HORIZON_WEB_SEARCH_TIMEOUT_SECONDS", "2")
    monkeypatch.setattr(enricher_module, "DDGS", FakeDDGS)

    results = asyncio.run(ContentEnricher(SimpleNamespace())._web_search("agent news", max_results=1))

    assert captured == {"timeout": 2, "query": "agent news", "max_results": 1}
    assert results == [{"title": "Result", "url": "https://example.com", "body": "Body"}]


def test_web_search_returns_empty_list_on_timeout(monkeypatch):
    async def slow_to_thread(*args, **kwargs):
        await asyncio.sleep(0.05)
        return [{"title": "late"}]

    monkeypatch.setenv("HORIZON_WEB_SEARCH_TIMEOUT_SECONDS", "0")
    monkeypatch.setattr(enricher_module.asyncio, "to_thread", slow_to_thread)

    results = asyncio.run(ContentEnricher(SimpleNamespace())._web_search("slow query"))

    assert results == []
