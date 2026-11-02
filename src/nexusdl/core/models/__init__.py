"""Public API of the NexusDL domain models.

The :mod:`nexusdl.core.models` package defines every **domain value object**
the framework passes between layers: the manga, the chapter, the page, the
site, the user, the search result, the download task and result, the
reading progress, and every enumeration those objects carry.

Architectural role
------------------
The models package is the **leaf of the domain dependency graph**. Every
model depends only on :mod:`nexusdl.core.models.enums` (and, in the case
of :class:`~nexusdl.core.models.download.DownloadTask`, on
:class:`~nexusdl.core.models.manga.Manga` and
:class:`~nexusdl.core.models.chapter.Chapter`, which themselves depend
only on enums). No model imports from ``core.library``, ``core.downloader``,
``core.image``, ``core.session``, ``core.registry``, ``parsers``,
``plugins``, or ``interfaces``.

That leaf position is what makes the package safe to import from anywhere:
a parser, a plugin, the database layer, the worker, and every composition
root all import models, and none of them creates a cycle by doing so.

Seven modules
-------------
* :mod:`~nexusdl.core.models.enums` — :class:`Language`,
  :class:`MangaStatus`, :class:`ContentRating`, :class:`PackagingFormat`,
  :class:`Priority`, :class:`DownloadStatus`, :class:`SiteHealth`,
  :class:`UserRole`. The absolute leaves — every other module in the
  package imports these.
* :mod:`~nexusdl.core.models.page` — :class:`Page`. The smallest unit
  the pipeline moves around; zero imports.
* :mod:`~nexusdl.core.models.manga` — :class:`Manga`. The central content
  entity.
* :mod:`~nexusdl.core.models.chapter` — :class:`Chapter`. The unit the
  worker downloads.
* :mod:`~nexusdl.core.models.user` — :class:`User`. The account model,
  with ``password_hash`` redacted in its repr and excluded from
  :meth:`User.to_public_dict`.
* :mod:`~nexusdl.core.models.site` — :class:`SiteConfig`,
  :class:`SiteCapabilities`, :class:`SiteHealthSnapshot`. Static site
  configuration plus runtime health.
* :mod:`~nexusdl.core.models.search` — :class:`SearchFilters`,
  :class:`SearchResult`, :class:`SearchResultPage`. The **search pipeline
  request/response shapes**.
* :mod:`~nexusdl.core.models.download` — :class:`DownloadTask`,
  :class:`DownloadResult`. The **download pipeline request/response
  shapes**.
* :mod:`~nexusdl.core.models.library` — :class:`ReadingProgress`,
  :class:`LibraryStats`. Aggregate library state.

Usage
-----
The most common import pattern from application code::

    from nexusdl.core.models import (
        Chapter,
        ContentRating,
        Language,
        Manga,
        MangaStatus,
        PackagingFormat,
        Priority,
    )

    manga = await parser.get_manga(url)
    if manga.content_rating is not ContentRating.SAFE:
        ...

    for chapter in await parser.get_chapters(manga):
        task = DownloadTask(
            manga=manga,
            chapter=chapter,
            format=PackagingFormat.CBZ,
            priority=Priority.NORMAL,
        )

For search plumbing::

    from nexusdl.core.models import (
        SearchFilters,
        SearchResult,
        SearchResultPage,
    )

For download pipeline plumbing::

    from nexusdl.core.models import DownloadResult, DownloadTask

Stability
---------
``__all__`` is the public contract. Adding a symbol is a minor change;
removing or renaming one is a breaking change.

The following are **not** re-exported because they are private to their
module and are allowed to change without notice:

* ``_SITE_ID_PATTERN``, ``_HEX16_PATTERN``, ``_MANGA_ID_PATTERN``,
  ``_INVALID_FILENAME_CHARS``, ``_HTTPS_PREFIX``, ``_TERMINAL_STATUSES``,
  ``_MANGA_ID_LENGTH``, ``_ID_LENGTH`` — compiled regexes and numeric
  constants.
* ``_validate_*`` helpers in :mod:`~nexusdl.core.models.manga` and
  :mod:`~nexusdl.core.models.chapter` — private to their class's
  ``__post_init__``.
* ``_utcnow`` in :mod:`~nexusdl.core.models.user` — module-private.

Dependency graph (no cycles)::

    enums.py       (feuille absolue — stdlib seulement)
        ▲
        │
    ┌───┴─────┬────────┬────────┬────────┬─────────┐
    │         │        │        │        │         │
  page     user     manga   chapter   site    search   library
   (leaf)  (enums)  (enums)  (enums)  (enums) (enums)  (leaf)
                                        ▲
                                        │
                                  download
                            (manga + chapter + enums)

`__init__.py` imports each submodule in leaf-to-root order so that a
`from nexusdl.core.models import X` never triggers a partially-initialized
module: enums first, then the leaves, then the composites.
"""

from __future__ import annotations

# --- Enumerations (leaf, imported by everything below) --------------------- #
from nexusdl.core.models.enums import (
    ContentRating,
    DownloadStatus,
    Language,
    MangaStatus,
    PackagingFormat,
    Priority,
    SiteHealth,
    UserRole,
)

# --- Standalone value objects (no in-package imports) --------------------- #
from nexusdl.core.models.library import LibraryStats, ReadingProgress
from nexusdl.core.models.page import Page

# --- Single-parent models (depend on enums only) -------------------------- #
from nexusdl.core.models.chapter import Chapter
from nexusdl.core.models.manga import Manga
from nexusdl.core.models.search import (
    SearchFilters,
    SearchResult,
    SearchResultPage,
)
from nexusdl.core.models.site import (
    SiteCapabilities,
    SiteConfig,
    SiteHealthSnapshot,
)
from nexusdl.core.models.user import User

# --- Composite models (depend on manga + chapter) ------------------------- #
from nexusdl.core.models.download import DownloadResult, DownloadTask

__all__ = [
    # --- Enums ----------------------------------------------------------- #
    "ContentRating",
    "DownloadStatus",
    "Language",
    "MangaStatus",
    "PackagingFormat",
    "Priority",
    "SiteHealth",
    "UserRole",
    # --- Core content ---------------------------------------------------- #
    "Chapter",
    "Manga",
    "Page",
    # --- Site ------------------------------------------------------------ #
    "SiteCapabilities",
    "SiteConfig",
    "SiteHealthSnapshot",
    # --- User ------------------------------------------------------------ #
    "User",
    # --- Search pipeline ------------------------------------------------- #
    "SearchFilters",
    "SearchResult",
    "SearchResultPage",
    # --- Download pipeline ---------------------------------------------- #
    "DownloadResult",
    "DownloadTask",
    # --- Library state --------------------------------------------------- #
    "LibraryStats",
    "ReadingProgress",
]
