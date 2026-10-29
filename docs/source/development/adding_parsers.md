# Adding a Parser

> **Audience**: contributors who want to implement the scraping logic for a
> **new site**.
>
> **Prerequisites**: read [Adding a Site](adding_sites.md) first. You must
> have a `sites.yaml` entry before implementing a parser.
>
> See also: [Architecture](architecture.md), [ADR-003: Parser Factory DI](decisions/003-parser-factory-di.md).

Adding a site to NexusDL has **two parts**:

1. **Declare the site in `data/sites.yaml`** — metadata, domains,
   capabilities. → See [Adding a Site](adding_sites.md). **Do this first.**
2. **Implement the parser** in `src/nexusdl/parsers/<language>/<site>.py`
   — scraping logic. **This page.**

```{note}
If your site is a **WordPress Madara**, **MangaThemesia**, **FoolSlide**,
or **REST/GraphQL API** site, use a [mixin](#mixins) — you'll write
~50 lines instead of ~500.
```

---

## Table of contents

1. [Prerequisites](#prerequisites)
2. [The parser anatomy](#the-parser-anatomy)
3. [The 4 abstract methods](#the-4-abstract-methods)
4. [Mixins](#mixins)
5. [Cloudflare and JS-rendered sites](#cloudflare-and-js-rendered-sites)
6. [Authentication](#authentication)
7. [Rate limiting](#rate-limiting)
8. [Error handling](#error-handling)
9. [Debugging](#debugging)
10. [Testing](#testing)
11. [Submit a PR](#submit-a-pr)

---

## Prerequisites

Before writing a parser, you must have:

1. **A `sites.yaml` entry** — see [Adding a Site](adding_sites.md). Without
   it, the parser is unreachable.
2. **A stable understanding of the site's structure** — open the site in
   a browser, inspect the HTML, and identify:
   - **Search URL** (`https://mysite.example.com/search?q=...`)
   - **Manga page URL** (`https://mysite.example.com/manga/one-piece`)
   - **Chapter list** (usually on the manga page, or a separate URL)
   - **Chapter page** (`https://mysite.example.com/manga/one-piece/chapter-1`)
   - **Image URLs** (usually `<img>` tags in the chapter page)
3. **The site's category** — Madara, MangaThemesia, API, Cloudflare, JS-rendered, or custom.
   See [Choosing a category](#choosing-a-category).

---

## The parser anatomy

A parser is a **Python class** that inherits from `BaseParser` (or a
mixin), and implements **4 async methods**:

```python
# src/nexusdl/parsers/en/mysite.py
from __future__ import annotations

from typing import TYPE_CHECKING

from nexusdl.parsers.base import BaseParser
from nexusdl.parsers.factory import register

if TYPE_CHECKING:
    from nexusdl.core.models.chapter import Chapter
    from nexusdl.core.models.manga import Manga
    from nexusdl.core.models.page import Page
    from nexusdl.core.models.search import SearchResult


class MySiteParser(BaseParser):
    """Parser for MySite."""

    site_id = "mysite"
    language = "en"
    adult = False

    async def search(self, query: str, *, page: int = 1) -> list[SearchResult]:
        """Search mangas matching the query.

        Args:
            query: Search query.
            page: Page number (1-based).

        Returns:
            List of search results.

        Raises:
            ParserError: If the site is unreachable or the HTML is invalid.
        """
        ...

    async def get_manga(self, url_or_id: str) -> Manga:
        """Fetch full manga metadata.

        Args:
            url_or_id: Manga URL or site-specific ID.

        Returns:
            Manga metadata.

        Raises:
            ParserError: If the manga is not found or the HTML is invalid.
        """
        ...

    async def get_chapters(self, manga: Manga) -> list[Chapter]:
        """Fetch the chapter list.

        Args:
            manga: The manga.

        Returns:
            List of chapters, sorted by number (ascending).

        Raises:
            ParserError: If the chapter list cannot be fetched.
        """
        ...

    async def get_pages(self, chapter: Chapter) -> list[Page]:
        """Fetch the page URLs of a chapter.

        Args:
            chapter: The chapter.

        Returns:
            List of pages, sorted by index (ascending).

        Raises:
            ParserError: If the pages cannot be fetched.
        """
        ...


# Register the parser (required — see ADR-003)
register("mysite", MySiteParser)
```

**Key rules:**

- ✅ `site_id` **must match** the `sites.yaml` `id`
- ✅ `language` **must match** the `sites.yaml` `language`
- ✅ `adult` **must match** the `sites.yaml` `adult`
- ✅ All 4 methods are **async**
- ✅ All 4 methods have **complete docstrings**
- ✅ `register()` is called **at module level**
- ❌ No `print()` — use `self._log`
- ❌ No hardcoded URL — use `self._base_url` (from `sites.yaml`)
- ❌ No blocking I/O — use `await self._session.get(...)`

---

## The 4 abstract methods

### `search(query, *, page=1)`

**Purpose**: Return a list of `SearchResult` matching the query.

**Typical pattern** (HTML site):

```python
async def search(self, query: str, *, page: int = 1) -> list[SearchResult]:
    url = f"{self._base_url}/search"
    html = await self._session.get_html(url, params={"q": query, "page": page})

    soup = BeautifulSoup(html, "lxml")
    results: list[SearchResult] = []

    for card in soup.select(".manga-card"):
        link = card.select_one("a")
        img = card.select_one("img")
        title = card.select_one(".title").get_text(strip=True)

        results.append(
            SearchResult(
                site_id=self.site_id,
                site_name=self.site_name,
                manga_id=self._extract_id(link["href"]),
                title=title,
                cover_url=img.get("src") if img else None,
                author=card.select_one(".author").get_text(strip=True) if card.select_one(".author") else None,
                language=self.language_enum,
                content_rating=self.content_rating,
                url=self._resolve_url(link["href"]),
            )
        )

    return results
```

**Typical pattern** (API site):

```python
async def search(self, query: str, *, page: int = 1) -> list[SearchResult]:
    response = await self._session.get_json(
        f"{self._base_url}/api/search",
        params={"q": query, "page": page},
    )

    return [
        SearchResult(
            site_id=self.site_id,
            site_name=self.site_name,
            manga_id=item["id"],
            title=item["title"],
            cover_url=item.get("cover"),
            author=item.get("author"),
            language=self.language_enum,
            content_rating=self.content_rating,
            url=self._resolve_url(f"/manga/{item['id']}"),
        )
        for item in response["results"]
    ]
```

**Edge cases:**

- Empty results → return `[]` (not `None`)
- Site returns 0 results but with a message → return `[]`
- Site pagination out of range → return `[]`

### `get_manga(url_or_id)`

**Purpose**: Return full `Manga` metadata (including `chapters=[]` — the
chapters are fetched separately).

**Typical pattern**:

```python
async def get_manga(self, url_or_id: str) -> Manga:
    url = self._resolve_url(url_or_id)
    html = await self._session.get_html(url)

    soup = BeautifulSoup(html, "lxml")

    title = soup.select_one("h1.title").get_text(strip=True)
    author = soup.select_one(".author").get_text(strip=True)
    description = soup.select_one(".description").get_text(strip=True)
    cover = soup.select_one(".cover img")["src"]
    status_text = soup.select_one(".status").get_text(strip=True).lower()

    status_map = {
        "ongoing": MangaStatus.ONGOING,
        "completed": MangaStatus.COMPLETED,
        "hiatus": MangaStatus.HIATUS,
        "cancelled": MangaStatus.CANCELLED,
    }

    return Manga(
        id=self._make_manga_id(url),
        source_id=self._extract_id(url),
        site=self.site_id,
        title=title,
        author=author,
        description=description,
        status=status_map.get(status_text, MangaStatus.UNKNOWN),
        cover_url=self._resolve_url(cover),
        language=self.language_enum,
        content_rating=self.content_rating,
        url=url,
        updated_at=datetime.now(timezone.utc),
    )
```

**Rules:**

- ✅ `Manga.id` = `self._make_manga_id(url)` — deterministic hash
- ✅ `Manga.chapters` = `[]` (fetched separately)
- ✅ `Manga.updated_at` = `datetime.now(timezone.utc)` if unknown
- ✅ Handle missing fields gracefully (author, cover, description)
- ❌ Don't fetch chapters in `get_manga()` — that's `get_chapters()`'s job

### `get_chapters(manga)`

**Purpose**: Return the list of `Chapter` for a manga, **sorted ascending**.

**Typical pattern**:

```python
async def get_chapters(self, manga: Manga) -> list[Chapter]:
    html = await self._session.get_html(f"{manga.url}/chapters")
    soup = BeautifulSoup(html, "lxml")

    chapters: list[Chapter] = []
    for row in soup.select(".chapter-row"):
        link = row.select_one("a")
        number_text = row.select_one(".chapter-number").get_text(strip=True)
        date_text = row.select_one(".date").get_text(strip=True)

        chapters.append(
            Chapter(
                id=self._make_chapter_id(manga.id, self._extract_chapter_id(link["href"])),
                manga_id=manga.id,
                source_id=self._extract_chapter_id(link["href"]),
                title=link.get_text(strip=True),
                number=self._parse_number(number_text),
                language=manga.language,
                url=self._resolve_url(link["href"]),
                published_at=self._parse_date(date_text),
            )
        )

    # Sort ascending by number
    chapters.sort(key=lambda c: self._sort_key(c.number))
    return chapters
```

**Rules:**

- ✅ **Sorted ascending** (chapter 1 first, chapter 100 last)
- ✅ `Chapter.id` = `self._make_chapter_id(...)` — deterministic hash
- ✅ `Chapter.manga_id` = `manga.id`
- ✅ Handle non-numeric chapters (`Extra`, `Bonus`, `0.5`, `12.5`)
- ✅ Handle missing `published_at` (set to `None`)
- ❌ Don't download pages in `get_chapters()` — that's `get_pages()`'s job

### `get_pages(chapter)`

**Purpose**: Return the list of `Page` for a chapter, **sorted by index**.

**Typical pattern (HTML)**:

```python
async def get_pages(self, chapter: Chapter) -> list[Page]:
    html = await self._session.get_html(chapter.url)
    soup = BeautifulSoup(html, "lxml")

    pages: list[Page] = []
    for idx, img in enumerate(soup.select(".reader img")):
        src = img.get("data-src") or img.get("src")
        if not src:
            continue
        pages.append(
            Page(
                index=idx,
                url=self._resolve_url(src),
                filename=f"{idx + 1:03d}.jpg",
            )
        )

    return pages
```

**Typical pattern (API)**:

```python
async def get_pages(self, chapter: Chapter) -> list[Page]:
    data = await self._session.get_json(f"{self._base_url}/api/chapter/{chapter.source_id}")

    return [
        Page(
            index=idx,
            url=page_url,
            filename=f"{idx + 1:03d}.jpg",
        )
        for idx, page_url in enumerate(data["pages"])
    ]
```

**Typical pattern (JS-rendered, using Playwright)**:

```python
async def get_pages(self, chapter: Chapter) -> list[Page]:
    if self._playwright is None:
        raise ParserError("Playwright required for this site")

    async with self._playwright.acquire() as ctx:
        page = await ctx.new_page()
        await page.goto(chapter.url)
        await page.wait_for_selector(".reader img")

        urls = await page.eval_on_selector_all(
            ".reader img",
            "imgs => imgs.map(img => img.dataset.src || img.src)",
        )

        await page.close()

    return [
        Page(index=idx, url=url, filename=f"{idx + 1:03d}.jpg")
        for idx, url in enumerate(urls)
    ]
```

**Rules:**

- ✅ **Sorted by `index`** (0-based, 0 first)
- ✅ `filename` is `001.jpg`, `002.jpg`, etc. (0-padded to 3 digits)
- ✅ Handle lazy-loaded images (`data-src` over `src`)
- ✅ Handle missing `data-src` (fall back to `src`)
- ✅ Raise `ParserError` if no pages are found
- ❌ Don't download images here — return URLs only

---

## Mixins

If your site is based on a **common CMS or theme**, use a **mixin** to
avoid reimplementing everything.

### Available mixins

| Mixin | Site category | Example sites |
|---|---|---|
| `WordPressMadaraMixin` | WordPress Madara theme | SushiScan, Scan-Manga, most FR sites |
| `MangaThemesiaMixin` | MangaThemesia theme | AsuraScans, FlameScans, most EN sites |
| `FoolSlideMixin` | FoolSlide CMS | Scan-Manga (legacy), some older FR sites |
| `ApiBasedMixin` | REST/GraphQL API | MangaDex, Comick |
| `CloudflareMixin` | Cloudflare-protected | nhentai, HentaiZone |
| `JsRenderedMixin` | JS-rendered (no CF) | Some EN sites |

### Using a mixin

```python
# src/nexusdl/parsers/fr/sushiscan.py
from nexusdl.parsers.mixins.wordpress_madara import WordPressMadaraMixin
from nexusdl.parsers.base import BaseParser
from nexusdl.parsers.factory import register


class SushiScanParser(WordPressMadaraMixin, BaseParser):
    """SushiScan parser (WordPress Madara).

    The mixin implements all 4 abstract methods. We only need to
    declare the site_id, language, and adult fields, and any
    site-specific overrides.
    """

    site_id = "sushiscan"
    language = "fr"
    adult = False

    # Optional: override CSS selectors if they differ from the default
    _search_selector = ".c-tabs-item__content"
    _chapter_selector = ".wp-manga-chapter"


register("sushiscan", SushiScanParser)
```

**That's it.** The mixin handles the 4 methods, pagination, error handling,
and HTML parsing.

### When to write a custom parser

Write a **custom parser** (no mixin) if:

- The site uses a **custom CMS** (not Madara, MangaThemesia, FoolSlide)
- The site has a **custom API** (REST/GraphQL with unusual shape)
- The site's HTML **doesn't match** any mixin's selectors
- You need **site-specific logic** (e.g. cookie refresh, login flow)

### Extending a mixin

You can **override** any method:

```python
class MyMadaraParser(WordPressMadaraMixin, BaseParser):
    site_id = "mysite"
    language = "en"
    adult = False

    async def get_pages(self, chapter: Chapter) -> list[Page]:
        """Override: this site uses a JS player instead of <img>."""
        html = await self._session.get_html(chapter.url)
        # Custom extraction logic
        ...
```

You can also **override CSS selectors** (class attributes) to adapt the
mixin to a site's minor HTML differences:

```python
class MyMadaraParser(WordPressMadaraMixin, BaseParser):
    _search_selector = ".my-custom-card"
    _title_selector = "h1.entry-title"
    _chapter_selector = ".chapter-item"
```

---

## Cloudflare and JS-rendered sites

If the site is behind **Cloudflare** or requires **JavaScript** to render,
you need one of two backends.

### Option A — Playwright (recommended)

Enable in `.env`:

```dotenv
NEXUSDL_PLAYWRIGHT_ENABLED=true
```

Install the browser:

```bash
playwright install chromium
```

In the parser, use `self._playwright`:

```python
async def get_pages(self, chapter: Chapter) -> list[Page]:
    if self._playwright is None:
        raise ParserError("Playwright required; enable NEXUSDL_PLAYWRIGHT_ENABLED")

    async with self._playwright.acquire() as ctx:
        page = await ctx.new_page()
        try:
            await page.goto(chapter.url, wait_until="domcontentloaded")
            await page.wait_for_selector(".reader img", timeout=30000)

            urls = await page.eval_on_selector_all(
                ".reader img",
                "imgs => imgs.map(img => img.dataset.src || img.src)",
            )
        finally:
            await page.close()

    return [Page(index=i, url=u, filename=f"{i + 1:03d}.jpg") for i, u in enumerate(urls)]
```

**Rules:**

- ✅ Always `await page.close()` in `finally`
- ✅ Use `wait_until="domcontentloaded"` (faster than `load`)
- ✅ Use `wait_for_selector` with a timeout
- ❌ Don't keep a page open between calls

### Option B — FlareSolverr

Enable in `.env`:

```dotenv
NEXUSDL_FLARESOLVERR_URL=http://localhost:8191/v1
```

Run FlareSolverr:

```bash
docker run -d --name flaresolverr -p 8191:8191 \
  ghcr.io/flaresolverr/flaresolverr:latest
```

In the parser, use `self._flaresolverr`:

```python
async def get_pages(self, chapter: Chapter) -> list[Page]:
    if self._flaresolverr is None:
        raise ParserError("FlareSolverr required; set NEXUSDL_FLARESOLVERR_URL")

    html = await self._flaresolverr.fetch_html(chapter.url)
    soup = BeautifulSoup(html, "lxml")

    return [
        Page(
            index=idx,
            url=self._resolve_url(img["src"]),
            filename=f"{idx + 1:03d}.jpg",
        )
        for idx, img in enumerate(soup.select(".reader img"))
    ]
```

**When to use which:**

| Situation | Use |
|---|---|
| Cloudflare challenge (5xx, "Just a moment...") | FlareSolverr (faster) or Playwright |
| JS-rendered content (SPA, React, Vue) | Playwright (required) |
| Cloudflare Turnstile | Playwright (better compatibility) |
| No display (CI, Docker headless) | FlareSolverr (no browser needed) |

---

## Authentication

If the site requires login (`requires_auth: true` in `sites.yaml`), use
`self._session.login()`:

```python
class MySiteParser(BaseParser):
    site_id = "mysite"
    language = "en"
    adult = False

    async def _ensure_logged_in(self) -> None:
        """Log in if not already logged in (idempotent)."""
        if getattr(self, "_logged_in", False):
            return
        await self._session.login(
            url=f"{self._base_url}/login",
            data={
                "username": self._settings.mysite_username,
                "password": self._settings.mysite_password.get_secret_value(),
            },
            success_selector=".user-menu",  # wait for this to appear
        )
        self._logged_in = True

    async def get_pages(self, chapter: Chapter) -> list[Page]:
        await self._ensure_logged_in()
        return await super().get_pages(chapter)
```

**Rules:**

- ✅ Credentials come from `self._settings.<site>_username` / `<site>_password`
- ✅ **Never** hardcode credentials
- ✅ Use `SecretStr.get_secret_value()` only when passing to the HTTP layer
- ✅ Make login **idempotent** (`_logged_in` flag)
- ✅ Cookies are persisted by `CookieManager` — no need to re-login on restart
- ❌ Don't log credentials (even at DEBUG level)

---

## Rate limiting

`HttpSession` already applies a **token bucket rate limiter** based on
`default_rate_limit_per_second` from `sites.yaml`. **You don't need to
implement rate limiting yourself.**

But you **can** add **additional** delays for aggressive sites:

```python
import asyncio

class MySiteParser(BaseParser):
    async def search(self, query: str, *, page: int = 1) -> list[SearchResult]:
        await asyncio.sleep(0.5)  # extra delay for this site
        ...
```

**Rules:**

- ✅ Respect the `default_rate_limit_per_second` from `sites.yaml`
- ✅ If a site is very fragile, add `await asyncio.sleep(...)`
- ❌ Don't bypass `HttpSession` and call `httpx` directly (you'd lose
  rate limiting, retries, caching, cookies)

---

## Error handling

Use **`ParserError`** and its subclasses from `parsers/exceptions.py`:

```python
from nexusdl.parsers.exceptions import (
    ParserError,
    ParserResponseError,
    ParserParseError,
    ParserNotFoundError,
)
```

### When to raise what

| Exception | When |
|---|---|
| `ParserResponseError` | HTTP 4xx/5xx, site unreachable |
| `ParserParseError` | HTML structure changed, missing required field |
| `ParserNotFoundError` | Manga / chapter does not exist (404) |
| `ParserError` | Generic parser failure |

**Rules:**

- ✅ Raise `ParserResponseError` when `response.status_code >= 400`
- ✅ Raise `ParserParseError` when `soup.select_one(...)` returns `None`
- ✅ Include **context** in the error message (the URL, the selector)
- ✅ Let `HttpSession` handle retries (`tenacity`) — don't retry yourself
- ❌ Don't catch `Exception` and swallow it
- ❌ Don't raise generic `Exception` — use `ParserError` subclasses

### Example

```python
async def get_manga(self, url_or_id: str) -> Manga:
    url = self._resolve_url(url_or_id)
    html = await self._session.get_html(url)

    soup = BeautifulSoup(html, "lxml")
    title_node = soup.select_one("h1.title")
    if title_node is None:
        raise ParserParseError(f"Title not found on {url}")
    title = title_node.get_text(strip=True)
    ...
```

---

## Debugging

When a parser doesn't work, here's a systematic approach.

### 1. Enable verbose logging

```bash
NEXUSDL_LOG_LEVEL=DEBUG nexusdl search "one piece" --site mysite
```

You'll see:

```
DEBUG   nexusdl.parsers.en.mysite   Fetching https://mysite.example.com/search?q=one+piece
DEBUG   nexusdl.core.session.http   GET 200 https://mysite.example.com/search?q=one+piece
DEBUG   nexusdl.parsers.en.mysite   Parsed 20 results
```

### 2. Inspect the raw HTML

```python
async def search(self, query: str, *, page: int = 1) -> list[SearchResult]:
    url = f"{self._base_url}/search"
    html = await self._session.get_html(url, params={"q": query, "page": page})

    # Temporary debug — remove before committing
    self._log.debug(f"HTML length: {len(html)}")
    self._log.debug(f"HTML preview: {html[:500]}")
    ...
```

Or with `curl`:

```bash
curl -s "https://mysite.example.com/search?q=one+piece" | head -100
```

### 3. Test selectors in the browser

Open the site in a browser, open DevTools Console, and test:

```javascript
document.querySelectorAll(".manga-card").length
document.querySelector(".title").textContent
```

### 4. Test selectors with `selectolax`

```bash
python -c "
from selectolax.parser import HTMLParser
html = open('debug.html').read()
tree = HTMLParser(html)
print(len(tree.css('.manga-card')))
"
```

### 5. Check the response status

```python
response = await self._session.get(url, params={"q": query})
print(response.status_code, response.headers.get("content-type"))
print(response.text[:500])
```

If `status_code == 403` and `content-type` is `text/html`, the site is
likely blocking you (Cloudflare).

### 6. Verify `sites.yaml`

```bash
python scripts/validate_sites.py
```

### 7. Check that the parser is registered

```bash
nexusdl sites | grep mysite
```

If empty, the `register()` call is missing or the module doesn't import.

### 8. Use `nexusdl debug`

```bash
nexusdl debug site mysite search "one piece"
```

This shows every HTTP request, response, and parse step.

---

## Testing

### 1. Unit tests (no network)

Use `respx` to mock HTTP responses:

```python
# tests/unit/parsers/test_mysite.py
from pathlib import Path

import httpx
import pytest
import respx

from nexusdl.parsers.en.mysite import MySiteParser


FIXTURES = Path(__file__).parent.parent.parent / "fixtures"


@pytest.fixture
def parser(settings, session):
    return MySiteParser(settings.sites["mysite"], session)


@respx.mock
@pytest.mark.asyncio
async def test_search_parses_results(parser):
    html = (FIXTURES / "mysite_search.html").read_text()
    respx.get("https://mysite.example.com/search").mock(
        return_value=httpx.Response(200, text=html)
    )

    results = await parser.search("one piece")

    assert len(results) == 20
    assert results[0].title == "One Piece"
    assert results[0].manga_id == "abc123"
```

### 2. Fixtures

Save real HTML responses to `tests/fixtures/`:

```bash
curl -s "https://mysite.example.com/search?q=one+piece" \
  > tests/fixtures/mysite_search.html
```

Or from the browser: DevTools → Network → right-click → "Save response".

### 3. Smoke test (hits real network)

```python
# tests/unit/parsers/test_mysite_smoke.py
import pytest
from nexusdl.parsers.en.mysite import MySiteParser


@pytest.mark.smoke
@pytest.mark.integration
@pytest.mark.asyncio
async def test_search_returns_results(parser):
    results = await parser.search("one piece")
    assert len(results) > 0
    assert results[0].title


@pytest.mark.smoke
@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_manga(parser):
    manga = await parser.get_manga("https://mysite.example.com/manga/one-piece")
    assert manga.title == "One Piece"
    assert manga.author


@pytest.mark.smoke
@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_pages(parser, sample_chapter):
    pages = await parser.get_pages(sample_chapter)
    assert len(pages) > 0
    assert pages[0].url.startswith("https://")
```

**Smoke tests** run only:

- On **manual invocation** (`pytest -m smoke`)
- In a **scheduled CI workflow** (weekly, not on PRs)

They **don't** run on every PR — that would hammer the sites.

### 4. Coverage

```bash
pytest tests/unit/parsers/test_mysite.py --cov=nexusdl.parsers.en.mysite --cov-report=term-missing
```

Target: **70%+** coverage on the parser.

---

## Submit a PR

Follow [Contributing](contributing.md).

### PR checklist

- [ ] `sites.yaml` entry added (see [Adding a Site](adding_sites.md))
- [ ] Parser implemented (`src/nexusdl/parsers/<lang>/<site>.py`)
- [ ] `register()` called at module level
- [ ] 4 abstract methods implemented
- [ ] Docstrings on all methods (Google style)
- [ ] No hardcoded URLs (use `self._base_url`)
- [ ] No secrets (use `self._settings`)
- [ ] No `print()` (use `self._log`)
- [ ] `ruff check src tests` passes
- [ ] `ruff format --check src tests` passes
- [ ] `mypy src` passes
- [ ] `pytest tests/unit/parsers/test_mysite.py` passes
- [ ] `pytest tests/unit/parsers/test_mysite_smoke.py -m smoke` passes
- [ ] Manual test: `nexusdl download "..." --chapters 1` produces a valid CBZ
- [ ] Rate limit respected (`default_rate_limit_per_second` is realistic)
- [ ] `CHANGELOG.md` updated (if notable)

### PR description template

```markdown
## Add parser: MySite

- **Site ID**: `mysite`
- **Language**: `en`
- **Category**: `en/`
- **Mixin used**: `WordPressMadaraMixin`
- **Requires auth**: ❌ no
- **Requires Cloudflare**: ❌ no
- **Lines of code**: ~50 (mostly mixin)

### Sample URLs used for testing

- Search: `https://mysite.example.com/search?q=one+piece`
- Manga: `https://mysite.example.com/manga/one-piece`
- Chapter: `https://mysite.example.com/manga/one-piece/chapter-1`

### Implementation notes

- Used `WordPressMadaraMixin` — only overrode `_search_selector`.
- Site is stable, no Cloudflare.
- Tested with 20 mangas, 100 chapters.

### Known limitations

- `get_chapters()` doesn't handle `Bonus` chapters (they appear as `null`).
```

---

## See also

- [Adding a Site](adding_sites.md) — the `sites.yaml` entry (do this first)
- [Architecture](architecture.md) — how parsers fit in the system
- [ADR-003: Parser Factory DI](decisions/003-parser-factory-di.md) — how `register()` works
- [Plugins](plugins.md) — add a parser via a plugin (no core changes)
- [`BaseParser`](https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/src/nexusdl/parsers/base.py) — the abstract class
- [`sites.yaml`](https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/src/nexusdl/data/sites.yaml) — the current site list
- [`parsers/_template.py`](https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/src/nexusdl/parsers/custom/_template.py) — the reference template

---

*This page is part of NexusDL and is licensed under
[GPL-3.0-or-later](../../../LICENSE).*
