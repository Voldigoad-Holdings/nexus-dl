"""Parser for ToonFR — French webtoon/manga aggregator (WordPress Madara + Cloudflare).

ToonFR (``toonfr.com``) is a French-language webtoon and manga aggregator
that runs on the **Madara** WordPress theme, the same stack used by
x-manga.org, x-manga.net, scan-hentai.fr, hentaizone.xyz, toonily and
several other French and English scanlation sites.  It is additionally
fronted by **Cloudflare**, so requests may be challenged and must be routed
through FlareSolverr / Playwright when needed.

The site diverges from stock Madara in one critical way: manga pages are
served under ``/webtoon/{slug}/`` instead of the standard ``/manga/{slug}/``.
Search results therefore link to ``/webtoon/`` URLs, and the Madara mixin's
default search selector (``a[href*='/manga/']``) would return nothing.  This
module overrides :meth:`search` to target the correct selector while
delegating the three remaining operations (``get_manga``, ``get_chapters``,
``get_pages``) to the Madara mixin, which handles them correctly:

* Manga details    — ``GET  /webtoon/{slug}/``
* Chapter list     — ``POST /webtoon/{slug}/ajax/chapters/`` (standard
  Madara AJAX endpoint, confirmed in use by the site's own front-end)
* Page list        — ``GET  /webtoon/{slug}/{chapter-slug}/`` (standard
  Madara reader selectors)

The site is French-language and **adult** (NSFW), declared on the class so
the registry can filter it via ``Settings.content.allow_adult`` and the
``language`` filter of ``SiteRegistry.list_sites``.

Domain note
-----------
The ``domains`` list in ``data/sites.yaml`` is the single source of truth for
the base URL.  If the site migrates (as scanlation sites frequently do), only
``sites.yaml`` needs updating — this file stays untouched.
"""
from __future__ import annotations

from typing import AsyncIterator

from selectolax.parser import HTMLParser

from nexusdl.core.models.search import SearchResult
from nexusdl.parsers.base import BaseParser
from nexusdl.parsers.factory import register
from nexusdl.parsers.mixins.cloudflare import CloudflareMixin
from nexusdl.parsers.mixins.wordpress_madara import WordPressMadaraMixin

__all__ = ["ToonFrParser"]


class ToonFrParser(WordPressMadaraMixin, CloudflareMixin, BaseParser):
    """Parser for ToonFR (WordPress Madara + Cloudflare, adult, French)."""

    site_id: str = "toonfr"
    language: str = "fr"
    adult: bool = True

    # ------------------------------------------------------------------ #
    #  search                                                            #
    # ------------------------------------------------------------------ #
    async def search(
        self,
        query: str,
        *,
        page: int = 1,
    ) -> AsyncIterator[SearchResult]:
        """Search ToonFR and yield results page by page.

        ToonFR uses the standard Madara search route
        (``/?s={query}&post_type=wp-manga``) but returns result cards whose
        links point to ``/webtoon/{slug}/`` instead of the usual
        ``/manga/{slug}/``.  The Madara mixin's default selector
        (``a[href*='/manga/']``) would therefore match nothing, so this
        method overrides it with the ``/webtoon/`` selector while keeping
        the rest of the Madara card structure intact.

        Args:
            query: Search terms (title, author, etc.).
            page: 1-based page number.

        Yields:
            SearchResult instances for the current page.
        """
        url = f"{self._base_url}/?s={query}&post_type=wp-manga&page={page}"
        html = await self.session.get_html(url)
        tree = HTMLParser(html)

        # Madara renders search results as c-tabs-item__content cards.  Each
        # card contains a link to the manga (here /webtoon/), a title and a
        # cover image.
        for card in tree.css("div.c-tabs-item__content, div.page-item-detail"):
            link = card.css_first("a[href*='/webtoon/']")
            if link is None:
                continue

            href = link.attributes.get("href", "")
            if not href:
                continue

            title_node = card.css_first("h3 a, h4 a, .post-title")
            title = title_node.text(strip=True) if title_node is not None else ""

            cover_node = card.css_first("img")
            cover_url = ""
            if cover_node is not None:
                cover_url = (
                    cover_node.attributes.get("data-src")
                    or cover_node.attributes.get("src")
                    or ""
                )

            yield SearchResult(
                site_id=self.site_id,
                title=title or href,
                url=self._resolve_url(href),
                cover_url=self._resolve_url(cover_url) if cover_url else None,
                language=self.language,
                adult=self.adult,
            )


# Auto-registration — triggered when ``nexusdl.parsers.fr`` is imported,
# which in turn is imported by ``nexusdl.parsers``.  This keeps the factory
# populated without any manual wiring in the composition root.
register("toonfr", ToonFrParser)
