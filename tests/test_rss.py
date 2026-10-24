import httpx
import pytest
import respx

from backend.app.collectors.rss import RSSCollector, RSSSource, canonicalize_url


def test_canonicalize_url_removes_tracking_parameters() -> None:
    assert (
        canonicalize_url("HTTPS://Example.com/story/?utm_source=rss&article=123#top")
        == "https://example.com/story?article=123"
    )


@pytest.mark.asyncio
@respx.mock
async def test_rss_collector_extracts_and_normalizes_article() -> None:
    feed_url = "https://news.example/feed.xml"
    article_url = "https://news.example/story?utm_source=rss"
    respx.get(feed_url).mock(
        return_value=httpx.Response(
            200,
            text=f"""<?xml version='1.0'?><rss><channel><item>
              <title>Example  headline</title><link>{article_url}</link>
              <pubDate>Tue, 21 May 2024 12:00:00 GMT</pubDate>
            </item></channel></rss>""",
        )
    )
    respx.get(article_url).mock(
        return_value=httpx.Response(
            200,
            text="<html><body><main><p>This is the verified article body.</p></main></body></html>",
        )
    )
    source = RSSSource(id="example", name="Example", feed_url=feed_url, category="world")
    async with httpx.AsyncClient() as client:
        articles, warnings = await RSSCollector(client, max_articles=10).collect(source)

    assert warnings == []
    assert len(articles) == 1
    assert articles[0].canonical_url == "https://news.example/story"
    assert "verified article body" in articles[0].normalized_text
