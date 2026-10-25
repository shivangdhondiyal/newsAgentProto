import httpx
import pytest
import respx

from backend.app.collectors.rss import (
    RSSCollector,
    RSSSource,
    canonicalize_url,
    strip_html,
)


def test_canonicalize_url_removes_tracking_parameters() -> None:
    assert (
        canonicalize_url("HTTPS://Example.com/story/?utm_source=rss&article=123#top")
        == "https://example.com/story?article=123"
    )


def test_strip_html_returns_visible_text() -> None:
    assert strip_html("<p>Hello &amp; <b>world</b></p>") == "Hello & world"
    assert strip_html("<script>ignore()</script><p>Keep this.</p>") == "Keep this."


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


@pytest.mark.asyncio
@respx.mock
async def test_rss_collector_strips_summary_markup_and_extraction_labels() -> None:
    feed_url = "https://news.example/feed.xml"
    article_url = "https://news.example/story"
    respx.get(feed_url).mock(
        return_value=httpx.Response(
            200,
            text=(
                "<?xml version='1.0'?><rss><channel><item>"
                "<title>Example &amp; headline</title>"
                f"<link>{article_url}</link>"
                "<description><![CDATA[<p>Summary with <b>markup</b> &amp; entities.</p>]]>"
                "</description></item></channel></rss>"
            ),
        )
    )
    respx.get(article_url).mock(
        return_value=httpx.Response(
            200,
            text=(
                "<html><body><main><p>- Published This is the verified body.</p>"
                "<p>Second sentence.</p></main></body></html>"
            ),
        )
    )
    source = RSSSource(id="example", name="Example", feed_url=feed_url, category="world")
    async with httpx.AsyncClient() as client:
        articles, warnings = await RSSCollector(client, max_articles=10).collect(source)

    assert warnings == []
    assert len(articles) == 1
    assert articles[0].title == "Example & headline"
    assert articles[0].summary == "Summary with markup & entities."
    assert "Published" not in articles[0].normalized_text
    assert articles[0].normalized_text.startswith(
        "This is the verified body. Second sentence."
    )


@pytest.mark.asyncio
@respx.mock
async def test_rss_collector_reports_skipped_entry_exception_type() -> None:
    feed_url = "https://news.example/feed.xml"
    article_url = "https://news.example/broken"
    respx.get(feed_url).mock(
        return_value=httpx.Response(
            200,
            text=f"""<?xml version='1.0'?><rss><channel><item>
              <title>Broken story</title><link>{article_url}</link>
            </item></channel></rss>""",
        )
    )
    respx.get(article_url).mock(return_value=httpx.Response(500, text="server error"))
    source = RSSSource(id="example", name="Example", feed_url=feed_url, category="world")
    async with httpx.AsyncClient() as client:
        articles, warnings = await RSSCollector(client, max_articles=10).collect(source)

    assert articles == []
    assert len(warnings) == 1
    assert "HTTPStatusError" in warnings[0]
