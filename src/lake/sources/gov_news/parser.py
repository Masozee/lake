"""Pure HTML parsing. selectolax is ~10x faster than BeautifulSoup and has no
lxml build dependency, which matters on a NUC."""

from __future__ import annotations

from urllib.parse import urljoin

from selectolax.parser import HTMLParser


def extract_pdf_links(html: bytes, base_url: str, limit: int = 50) -> list[str]:
    """Absolute URLs of PDFs linked from an index page, de-duplicated, order kept."""
    tree = HTMLParser(html.decode("utf-8", errors="replace"))
    seen: dict[str, None] = {}

    for node in tree.css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        if not href or not href.lower().split("?")[0].endswith(".pdf"):
            continue
        seen.setdefault(urljoin(base_url, href), None)
        if len(seen) >= limit:
            break

    return list(seen)


def parse_index(html: bytes, base_url: str) -> list[dict]:
    """Article metadata from the index page. Cheap, and enough to build a table."""
    tree = HTMLParser(html.decode("utf-8", errors="replace"))
    items = []

    for article in tree.css("article, li.news-item, div.news-item"):
        title_node = article.css_first("h2, h3, a")
        link_node = article.css_first("a[href]")
        time_node = article.css_first("time")
        if not title_node:
            continue
        items.append(
            {
                "title": title_node.text(strip=True),
                "url": urljoin(base_url, link_node.attributes.get("href", ""))
                if link_node
                else None,
                "published_at": (time_node.attributes.get("datetime") if time_node else None),
            }
        )
    return items
