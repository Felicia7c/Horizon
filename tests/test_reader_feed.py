from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.models import ContentItem, ReaderFeedConfig, SourceType
from src.services.reader_feed import ReaderFeedGenerator


def _make_item() -> ContentItem:
    item = ContentItem(
        id="rss:example:1",
        source_type=SourceType.RSS,
        title="Ghostty is leaving GitHub",
        url="https://example.com/ghostty",
        author="tester",
        published_at=datetime(2026, 4, 29, 8, 0, tzinfo=timezone.utc),
        metadata={
            "title_zh": "Ghostty 将离开 GitHub",
            "detailed_summary_zh": "Ghostty 宣布将迁出 GitHub，原因是项目治理和平台依赖问题。",
            "background_zh": "Ghostty 是一个现代终端模拟器，受到开发者社区关注。",
            "community_discussion_zh": "社区讨论集中在开源项目对 GitHub 的依赖。",
        },
    )
    item.ai_score = 8.4
    item.ai_summary = "Ghostty is moving away from GitHub."
    item.ai_tags = ["devtools", "open-source"]
    return item


def test_reader_feed_renders_one_item_per_story():
    config = ReaderFeedConfig(title="My Horizon", base_url="https://news.example.com")
    generator = ReaderFeedGenerator(config)

    xml = generator.build_feed([_make_item()], date="2026-04-29", language="zh")

    assert '<rss version="2.0"' in xml
    assert "<title>My Horizon - ZH</title>" in xml
    assert "<title>Ghostty 将离开 GitHub</title>" in xml
    assert "<link>https://example.com/ghostty</link>" in xml
    assert "<guid isPermaLink=\"false\">horizon:zh:rss:example:1</guid>" in xml
    assert "<category>devtools</category>" in xml
    assert "<category>open-source</category>" in xml
    assert "AI Score: 8.4" in xml
    assert "Ghostty 宣布将迁出 GitHub" in xml
    assert "社区讨论集中在开源项目对 GitHub 的依赖" in xml
    assert 'href="https://example.com/ghostty"' in xml
    assert '<atom:link href="https://news.example.com/feeds/horizon-selected-zh.xml"' in xml


def test_reader_feed_writes_data_and_docs_outputs(tmp_path: Path):
    config = ReaderFeedConfig(
        enabled=True,
        title="My Horizon",
        output_dir=str(tmp_path / "data-feeds"),
        docs_output_dir=str(tmp_path / "docs-feeds"),
    )
    generator = ReaderFeedGenerator(config)

    paths = generator.save_feed([_make_item()], date="2026-04-29", language="zh")

    assert paths == [
        tmp_path / "data-feeds" / "horizon-selected-zh.xml",
        tmp_path / "docs-feeds" / "horizon-selected-zh.xml",
    ]
    assert paths[0].exists()
    assert paths[1].exists()
    assert "Ghostty 将离开 GitHub" in paths[0].read_text(encoding="utf-8")
