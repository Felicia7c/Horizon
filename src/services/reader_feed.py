"""RSS feed output for phone reading apps."""

from __future__ import annotations

from email.utils import format_datetime
from html import escape
from pathlib import Path
from typing import Iterable

from ..models import ContentItem, ReaderFeedConfig


def _cdata(value: str) -> str:
    return value.replace("]]>", "]]]]><![CDATA[>")


def _published_at(item: ContentItem) -> str:
    published_at = item.published_at
    if published_at.tzinfo is None:
        from datetime import timezone

        published_at = published_at.replace(tzinfo=timezone.utc)
    return format_datetime(published_at)


def _text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


class ReaderFeedGenerator:
    """Generates RSS 2.0 feeds from Horizon-selected items."""

    def __init__(self, config: ReaderFeedConfig):
        self.config = config

    def feed_filename(self, language: str) -> str:
        return f"{self.config.feed_slug}-{language}.xml"

    def should_emit_language(self, language: str) -> bool:
        return not self.config.languages or language in self.config.languages

    def _self_url(self, language: str) -> str | None:
        if not self.config.base_url:
            return None
        return f"{self.config.base_url.rstrip('/')}/feeds/{self.feed_filename(language)}"

    def _channel_link(self) -> str:
        return self.config.base_url.rstrip("/") if self.config.base_url else "https://horizon.local/"

    def build_feed(
        self,
        items: Iterable[ContentItem],
        *,
        date: str,
        language: str,
    ) -> str:
        selected_items = list(items)[: self.config.max_items]
        self_url = self._self_url(language)
        atom_link = (
            f'    <atom:link href="{escape(self_url)}" rel="self" type="application/rss+xml"/>\n'
            if self_url
            else ""
        )
        item_xml = "\n".join(
            self._build_item(item, date=date, language=language) for item in selected_items
        )

        return (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n'
            "  <channel>\n"
            f"    <title>{escape(self.config.title)} - {escape(language.upper())}</title>\n"
            f"    <link>{escape(self._channel_link())}</link>\n"
            f"    <description>{escape(self.config.description)}</description>\n"
            f"    <lastBuildDate>{escape(date)}</lastBuildDate>\n"
            f"{atom_link}"
            f"{item_xml}\n"
            "  </channel>\n"
            "</rss>\n"
        )

    def save_feed(
        self,
        items: Iterable[ContentItem],
        *,
        date: str,
        language: str,
    ) -> list[Path]:
        xml = self.build_feed(items, date=date, language=language)
        targets = [Path(self.config.output_dir) / self.feed_filename(language)]
        if self.config.docs_output_dir:
            targets.append(Path(self.config.docs_output_dir) / self.feed_filename(language))

        for target in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(xml, encoding="utf-8")
        return targets

    def _build_item(self, item: ContentItem, *, date: str, language: str) -> str:
        meta = item.metadata or {}
        title = (
            _text(meta.get(f"title_{language}"))
            or _text(meta.get("title"))
            or item.title
        )
        summary = (
            _text(meta.get(f"detailed_summary_{language}"))
            or _text(meta.get("detailed_summary"))
            or _text(item.ai_summary)
            or _text(item.content)
        )
        background = _text(meta.get(f"background_{language}")) or _text(meta.get("background"))
        discussion = _text(meta.get(f"community_discussion_{language}")) or _text(
            meta.get("community_discussion")
        )
        source_url = str(item.url)
        tags = "\n".join(f"      <category>{escape(tag)}</category>" for tag in item.ai_tags)

        html_parts = [
            f"<p>AI Score: {escape(str(item.ai_score or 'N/A'))}</p>",
            f"<p>{escape(summary)}</p>",
        ]
        if background:
            html_parts.append(f"<p><strong>Background:</strong> {escape(background)}</p>")
        if discussion:
            html_parts.append(f"<p><strong>Community:</strong> {escape(discussion)}</p>")
        if item.ai_tags:
            html_parts.append(
                "<p><strong>Tags:</strong> "
                + ", ".join(f"#{escape(tag)}" for tag in item.ai_tags)
                + "</p>"
            )
        html_parts.append(f'<p><a href="{escape(source_url)}">Read original</a></p>')
        description = "\n".join(html_parts)

        return (
            "    <item>\n"
            f"      <title>{escape(title)}</title>\n"
            f"      <link>{escape(source_url)}</link>\n"
            f'      <guid isPermaLink="false">horizon:{escape(language)}:{escape(item.id)}</guid>\n'
            f"      <pubDate>{escape(_published_at(item))}</pubDate>\n"
            f"      <description><![CDATA[{_cdata(description)}]]></description>\n"
            f"{tags}\n"
            "    </item>"
        )
