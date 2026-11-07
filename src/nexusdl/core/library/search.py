"""Full-text search over the local library, backed by SQLite FTS5.

The library keeps manga metadata in the ``mangas`` table with an external-
content FTS5 index (``mangas_fts``, created by
``migrations/001_initial.sql``). This module is the *only* place that
composes and executes FTS5 queries against that index, and it is the
*only* place that decides how a user query string becomes a well-formed
FTS5 MATCH expression.

Two public layers
-----------------
* **Query building** — pure functions that turn a user string into safe,
  parameterized SQL fragments: :func:`build_fts_query` and
  :func:`build_like_patterns`. Testable in isolation, no DB required.
* **Execution** — :class:`LibrarySearch`, a thin object that runs the built
  queries against a caller-provided session factory and hydrates the rows
  back into :class:`~nexusdl.core.models.manga.Manga` objects through a
  caller-provided mapper.

The split exists because query building is where bugs hide (FTS5 operator
injection, LIKE wildcard injection, phrase/prefix semantics), and query
building is far easier to unit-test as pure functions than as SQL executed
against a live database.

Query syntax
------------
The user-facing query syntax is deliberately small and matches what manga
readers expect:

* ``one two`` — two terms, implicitly AND-ed.
* ``"one piece"`` — a phrase. The quotes are preserved; the interior is
  escaped so an embedded ``"`` cannot terminate the phrase early.
* ``one*`` — a prefix. The term before ``*`` must be a single word
  (letters, digits, underscore, or CJK); a term with punctuation
  (``one-two*``) is treated as a phrase instead, because FTS5 does not
  support prefix on multi-word terms.
* Any other operator FTS5 recognizes (``AND``, ``OR``, ``NOT``, ``NEAR``)
  is **quoted** and therefore treated as a literal term. Users who need
  these operators should be aware that plain searches are implicit-AND.

This is intentionally **not** a pass-through to FTS5 syntax. A pass-through
would let a user input like ``one(two`` (unbalanced parenthesis) raise an
``OperationalError`` from SQLite, which we would have to catch and
translate anyway. Building the expression ourselves means user input can
never produce a malformed MATCH — only a query that returns zero results.

FTS5 fallback
-------------
SQLite builds vary; some minimal builds ship without FTS5, and an
exceptionally unlucky install might have a ``mangas_fts`` table that was
dropped or renamed. :class:`LibrarySearch` detects FTS5 availability once
per instance (a single ``SELECT 1 FROM mangas_fts LIMIT 1`` probe),
caches the answer, and falls back to a **substring search** using ``LIKE``
with per-term AND semantics. The fallback:

* never throws for query-shape reasons (LIKE has no operators);
* loses relevance ranking (no ``bm25``) — results are ordered by
  ``created_at DESC, id ASC``;
* loses prefix semantics — a prefix token becomes an ordinary substring
  (``one*`` → ``%one%``) because LIKE has no prefix operator that works
  across word boundaries.

The trade-off is documented here rather than hidden in the query builder,
so an operator seeing "unexpected ordering" in a fallback-only environment
knows where to look.

Parameter binding and injection safety
--------------------------------------
Every dynamic value — the FTS5 MATCH expression, LIKE patterns, optional
filters, ``LIMIT``, ``OFFSET`` — is bound through SQLAlchemy's ``:name``
parameter syntax. No user input is ever interpolated into the SQL string
itself. Table and column names are compile-time literals from this module,
never from user input.

Dependency graph (no cycles)::

    core.exceptions            ─┐
    core.models.enums           ├──► core.library.search
    core.models.manga           │
    (AsyncSession via Protocol) ─┘

No ``parsers/``, ``plugins/``, or ``interfaces/`` dependency (rule R1).
No dependency on ``core.library.database`` — the coupling runs the other
way: ``database.py`` constructs a :class:`LibrarySearch` and provides it a
session factory and a hydration function. This keeps the whole library
package free of circular imports regardless of which module is written
first.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal

from loguru import logger
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError

from nexusdl.core.exceptions import LibraryError
from nexusdl.core.models.enums import ContentRating, Language, MangaStatus

if TYPE_CHECKING:
    from collections.abc import AsyncContextManager

    from sqlalchemy.ext.asyncio import AsyncSession

    from nexusdl.core.models.manga import Manga


__all__ = [
    "DEFAULT_LIMIT",
    "DEFAULT_MAX_LIMIT",
    "LibrarySearch",
    "PaginatedResult",
    "build_fts_query",
    "build_like_patterns",
]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Default page size when the caller does not specify ``limit``.
DEFAULT_LIMIT: Final[int] = 50

#: Hard ceiling on ``limit``. A caller asking for 100 000 rows almost
#: certainly wants a different mechanism (a report, an export), not search.
DEFAULT_MAX_LIMIT: Final[int] = 200

#: Escape character used in LIKE clauses. Deliberately not ``\\`` to avoid
#: Python string-literal double-escaping at every call site, and not a
#: character that appears in URLs or natural-language titles.
_LIKE_ESCAPE: Final[str] = "#"

#: Column names participating in the LIKE fallback. Order matters only for
#: the generated SQL, not for semantics — every column is OR-ed with every
#: other, then the whole block is AND-ed with the next term.
_LIKE_COLUMNS: Final[tuple[str, ...]] = (
    "title",
    "alt_titles",
    "author",
    "description",
)

#: FTS5 external-content table name. Must match ``mangas_fts`` in
#: ``migrations/001_initial.sql``.
_FTS_TABLE: Final[str] = "mangas_fts"

#: Tokenizer pattern matching the FTS5 ``unicode61`` tokenization that the
#: migrations configure: alphanumerics + underscore + CJK. Used only to
#: decide whether a token is safely usable as a bare FTS5 prefix
#: (``word*``). Everything else is treated as a phrase.
_SAFE_PREFIX_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9_\u00C0-\u024F\u4E00-\u9FFF\u3040-\u30FF\uAC00-\uD7AF]+$"
)


_log = logger.bind(component="library.search")


# --------------------------------------------------------------------------- #
# Paginated result                                                            #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PaginatedResult[T]:
    """Immutable page of results with pagination metadata.

    Generic over the item type so that both search (``Manga``) and future
    list endpoints (e.g. ``Chapter``, ``DownloadTask``) can reuse the same
    shape without duplicating the pagination math. The class is
    deliberately **not** tied to any database concern — it is a plain
    value object.

    Attributes:
        items: The page's items, in the caller's expected order.
        total: Total number of items matching the query across all pages.
        page: 1-indexed current page. Always ``1`` for an empty result
            (``total == 0``), regardless of the requested offset.
        per_page: Page size used for this query (equal to ``limit`` at the
            call site).
        total_pages: ``ceil(total / per_page)``, or ``0`` when ``total``
            is zero. A page value with ``total_pages == 0`` means "no
            results", not "page 1 of 0".
    """

    items: tuple[T, ...]
    total: int
    page: int
    per_page: int
    total_pages: int

    @property
    def has_next(self) -> bool:
        """``True`` iff there is at least one page after :attr:`page`."""
        return self.page < self.total_pages

    @property
    def has_prev(self) -> bool:
        """``True`` iff there is at least one page before :attr:`page`."""
        return self.page > 1


# --------------------------------------------------------------------------- #
# Query building — pure functions                                             #
# --------------------------------------------------------------------------- #


_TokenKind = Literal["term", "phrase", "prefix"]


@dataclass(frozen=True, slots=True)
class _Token:
    """A single lexical unit produced by :func:`_tokenize`."""

    kind: _TokenKind
    text: str


def build_fts_query(user_query: str) -> str | None:
    """Turn *user_query* into a safe FTS5 ``MATCH`` expression.

    Returns ``None`` when the query contains no searchable tokens (empty
    input, whitespace only, only unmatched quote characters), signalling
    "there is nothing to match" rather than "match everything".

    The returned string is safe to bind as an FTS5 parameter; it contains
    only canonical FTS5 syntax (double-quoted phrases, bare prefixes,
    implicit-AND operators between them). It is **not** intended to be
    interpolated into SQL — bind it as a named parameter.

    Semantics match the module docstring:

    * ``one two`` → ``"one" "two"`` (both AND-ed)
    * ``"one piece"`` → ``"one piece"`` (single phrase)
    * ``one*`` → ``one*`` (prefix)
    * ``one-two*`` → ``"one-two"`` (phrase; prefix is not valid on
      multi-token terms in FTS5)
    * ``AND`` (uppercase, standalone) → ``"AND"`` (quoted; operator
      semantics are deliberately not exposed to user input)

    Args:
        user_query: Raw query string from the caller (CLI argument, web
            request body, TUI search field).

    Returns:
        A well-formed FTS5 expression, or ``None`` when the query has no
        usable tokens.
    """
    tokens = _tokenize(user_query)
    if not tokens:
        return None

    parts: list[str] = []
    for token in tokens:
        if token.kind == "phrase":
            parts.append(_quote_phrase(token.text))
        elif token.kind == "prefix":
            if _SAFE_PREFIX_PATTERN.match(token.text):
                parts.append(f"{token.text}*")
            else:
                # Not a single token — degrade to a phrase so the query
                # still succeeds and returns a superset of the intended
                # matches.
                parts.append(_quote_phrase(token.text))
        else:  # "term"
            parts.append(_quote_phrase(token.text))

    if not parts:
        return None
    return " ".join(parts)


def build_like_patterns(user_query: str) -> tuple[str, ...]:
    """Turn *user_query* into LIKE patterns for the fallback path.

    Returns an empty tuple when there are no usable tokens — the caller
    treats that as "no results".

    Each pattern is already escaped for the module's LIKE escape character
    (:data:`_LIKE_ESCAPE`) and wrapped in ``%`` markers (substring match).
    Prefix tokens (``one*``) produce a **substring** pattern ``%one%`` for
    the fallback — LIKE has no word-boundary-aware prefix operator, so the
    fallback cannot preserve prefix semantics. Documented in the module
    docstring.

    Args:
        user_query: Raw query string from the caller.

    Returns:
        A tuple of escaped LIKE patterns. Bind each as a separate named
        parameter; the caller is responsible for interpolating ``ESCAPE``
        into the SQL (see :class:`LibrarySearch`).
    """
    tokens = _tokenize(user_query)
    if not tokens:
        return ()
    return tuple(
        _wrap_like(_escape_like(token.text)) for token in tokens
    )


# --------------------------------------------------------------------------- #
# LibrarySearch                                                               #
# --------------------------------------------------------------------------- #


class LibrarySearch:
    """Execute full-text searches against the library.

    One instance is created by
    :class:`~nexusdl.core.library.database.LibraryDatabase` and reused for
    the database's lifetime. The instance is stateless except for the
    ``_fts5_available`` cache; concurrent :meth:`search` calls are safe
    because the cache transition (``None → True``/``False``) is
    idempotent under any interleaving — a race only costs one extra
    probe.

    Example::

        search = LibrarySearch(
            session_factory=database.session,
            hydrate_manga=database._row_to_manga,
        )
        page = await search.search("one piece", limit=20, offset=0)
        for manga in page.items:
            ...

    Args:
        session_factory: Callable producing an async context manager that
            yields a SQLAlchemy :class:`~sqlalchemy.ext.asyncio.AsyncSession`.
            Typically ``LibraryDatabase.session``.
        hydrate_manga: Callable mapping a SQLAlchemy row mapping (dict-like,
            column name → value) to a
            :class:`~nexusdl.core.models.manga.Manga`. Typically
            ``LibraryDatabase._row_to_manga``. Called once per returned
            row; raising propagates (a mapper bug is a ``database.py``
            bug and should surface loudly).
    """

    def __init__(
        self,
        session_factory: Callable[[], AsyncContextManager[AsyncSession]],
        hydrate_manga: Callable[[Mapping[str, Any]], Manga],
    ) -> None:
        self._session_factory = session_factory
        self._hydrate_manga = hydrate_manga
        #: Lazy cache for FTS5 availability detection. ``None`` until the
        #: first search; thereafter ``True`` or ``False``. Once ``False``
        #: it stays ``False`` for the lifetime of this instance (a broken
        #: runtime is not expected to repair itself mid-session).
        self._fts5_available: bool | None = None

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #

    async def search(
        self,
        query: str,
        *,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
        language: Language | None = None,
        status: MangaStatus | None = None,
        content_rating: ContentRating | None = None,
    ) -> PaginatedResult[Manga]:
        """Search the library, returning a paginated, relevance-sorted page.

        Args:
            query: Raw user query string (see the module docstring for the
                supported syntax).
            limit: Page size, in ``[1, DEFAULT_MAX_LIMIT]``.
            offset: Zero-based offset into the result set. Must be
                ``>= 0`` and, for consistency with HTTP pagination, is
                expected to be a multiple of ``limit`` — the function does
                not enforce this, but misaligned offsets produce pages
                whose ``page`` field rounds down.
            language: Optional filter on :attr:`mangas.language`.
            status: Optional filter on :attr:`mangas.status`.
            content_rating: Optional filter on
                :attr:`mangas.content_rating`. Callers implementing an
                adult-content gate should exclude
                :attr:`ContentRating.PORNOGRAPHIC` /
                :attr:`ContentRating.EROTICA` at this layer — the search
                module does not apply a policy of its own.

        Returns:
            A :class:`PaginatedResult` of hydrated manga objects. When no
            row matches, ``items`` is empty and ``total_pages == 0``.

        Raises:
            ValueError: If ``limit`` is outside ``[1, DEFAULT_MAX_LIMIT]``
                or ``offset`` is negative.
            LibraryError: If the database rejects a well-formed query in a
                way that is not a runtime-availability problem (the
                runtime-availability case falls back to LIKE instead).
        """
        self._validate_pagination(limit, offset)

        if not query.strip():
            return self._empty_result(limit)

        fts_query = build_fts_query(query)
        if fts_query is None:
            return self._empty_result(limit)

        async with self._session_factory() as session:
            if await self._check_fts5(session):
                try:
                    return await self._search_fts5(
                        session,
                        fts_query,
                        limit=limit,
                        offset=offset,
                        language=language,
                        status=status,
                        content_rating=content_rating,
                    )
                except OperationalError as exc:
                    # A runtime that passed the FTS5 probe but failed on a
                    # real query is not recoverable in-session. Disable
                    # FTS5 for this instance and fall through to LIKE.
                    self._fts5_available = False
                    _log.warning(
                        "FTS5 query failed unexpectedly; falling back to "
                        "LIKE for the remainder of this session: {}",
                        exc,
                    )

        # Fallback uses a fresh session: the one above may be in a
        # partial or rollback-only state after the OperationalError.
        async with self._session_factory() as session:
            return await self._search_like(
                session,
                query,
                limit=limit,
                offset=offset,
                language=language,
                status=status,
                content_rating=content_rating,
            )

    # ------------------------------------------------------------------ #
    # Validation                                                         #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _validate_pagination(limit: int, offset: int) -> None:
        """Reject invalid ``limit`` / ``offset`` combinations up front.

        Raises:
            ValueError: If ``limit`` or ``offset`` is out of range.
        """
        if not (1 <= limit <= DEFAULT_MAX_LIMIT):
            raise ValueError(
                f"limit must be in [1, {DEFAULT_MAX_LIMIT}], got {limit}"
            )
        if offset < 0:
            raise ValueError(f"offset must be >= 0, got {offset}")

    @staticmethod
    def _empty_result(limit: int) -> PaginatedResult[Manga]:
        """Return an empty :class:`PaginatedResult`.

        ``page`` is forced to ``1`` — an empty result is not "page N of 0"
        even when the caller asked for a non-zero offset. This keeps the
        ``has_prev`` / ``has_next`` invariants coherent: an empty result
        has neither.
        """
        return PaginatedResult(
            items=(),
            total=0,
            page=1,
            per_page=limit,
            total_pages=0,
        )

    # ------------------------------------------------------------------ #
    # FTS5 availability detection                                        #
    # ------------------------------------------------------------------ #

    async def _check_fts5(self, session: AsyncSession) -> bool:
        """Return ``True`` iff the ``mangas_fts`` virtual table is usable.

        Performs one lightweight probe on first call and caches the result
        on the instance. Subsequent calls are O(1).
        """
        if self._fts5_available is not None:
            return self._fts5_available
        try:
            await session.execute(
                text(f"SELECT 1 FROM {_FTS_TABLE} LIMIT 1")
            )
        except (OperationalError, ProgrammingError) as exc:
            _log.debug(
                "FTS5 unavailable on this runtime ({}); using LIKE fallback",
                exc,
            )
            self._fts5_available = False
        else:
            self._fts5_available = True
        return self._fts5_available

    # ------------------------------------------------------------------ #
    # FTS5 path                                                          #
    # ------------------------------------------------------------------ #

    async def _search_fts5(
        self,
        session: AsyncSession,
        fts_query: str,
        *,
        limit: int,
        offset: int,
        language: Language | None,
        status: MangaStatus | None,
        content_rating: ContentRating | None,
    ) -> PaginatedResult[Manga]:
        """Run the search against the FTS5 index and hydrate the page."""
        filter_sql, filter_params = _build_filter_clause(
            language, status, content_rating, table_alias="m"
        )

        count_sql = (
            f"SELECT COUNT(*) FROM {_FTS_TABLE} "
            f"JOIN mangas m ON m.rowid = {_FTS_TABLE}.rowid "
            f"WHERE {_FTS_TABLE} MATCH :q {filter_sql}"
        )
        count_params: dict[str, Any] = {"q": fts_query, **filter_params}
        total_result = await session.execute(text(count_sql), count_params)
        total = int(total_result.scalar_one())

        if total == 0:
            return self._empty_result(limit)

        page_sql = (
            f"SELECT m.* FROM {_FTS_TABLE} "
            f"JOIN mangas m ON m.rowid = {_FTS_TABLE}.rowid "
            f"WHERE {_FTS_TABLE} MATCH :q {filter_sql} "
            f"ORDER BY bm25({_FTS_TABLE}), m.id ASC "
            f"LIMIT :limit OFFSET :offset"
        )
        page_params: dict[str, Any] = {
            "q": fts_query,
            "limit": limit,
            "offset": offset,
            **filter_params,
        }
        rows_result = await session.execute(text(page_sql), page_params)
        rows = rows_result.mappings().all()
        items = tuple(self._hydrate_manga(dict(row)) for row in rows)

        return PaginatedResult(
            items=items,
            total=total,
            page=offset // limit + 1,
            per_page=limit,
            total_pages=_total_pages(total, limit),
        )

    # ------------------------------------------------------------------ #
    # LIKE fallback                                                      #
    # ------------------------------------------------------------------ #

    async def _search_like(
        self,
        session: AsyncSession,
        user_query: str,
        *,
        limit: int,
        offset: int,
        language: Language | None,
        status: MangaStatus | None,
        content_rating: ContentRating | None,
    ) -> PaginatedResult[Manga]:
        """Run a substring-AND search against ``mangas`` without FTS5."""
        patterns = build_like_patterns(user_query)
        if not patterns:
            return self._empty_result(limit)

        params: dict[str, Any] = {}
        blocks: list[str] = []
        for i, pattern in enumerate(patterns):
            key = f"p{i}"
            params[key] = pattern
            ors = " OR ".join(
                f"{col} LIKE :{key} ESCAPE '{_LIKE_ESCAPE}'"
                for col in _LIKE_COLUMNS
            )
            blocks.append(f"({ors})")
        where_block = " AND ".join(blocks)

        filter_sql, filter_params = _build_filter_clause(
            language, status, content_rating, table_alias=""
        )
        params.update(filter_params)

        count_sql = (
            f"SELECT COUNT(*) FROM mangas WHERE {where_block} {filter_sql}"
        )
        total = int(
            (await session.execute(text(count_sql), params)).scalar_one()
        )
        if total == 0:
            return self._empty_result(limit)

        page_params = dict(params)
        page_params["limit"] = limit
        page_params["offset"] = offset
        page_sql = (
            f"SELECT * FROM mangas WHERE {where_block} {filter_sql} "
            f"ORDER BY created_at DESC, id ASC "
            f"LIMIT :limit OFFSET :offset"
        )
        rows = (
            (await session.execute(text(page_sql), page_params))
            .mappings()
            .all()
        )
        items = tuple(self._hydrate_manga(dict(row)) for row in rows)

        return PaginatedResult(
            items=items,
            total=total,
            page=offset // limit + 1,
            per_page=limit,
            total_pages=_total_pages(total, limit),
        )


# --------------------------------------------------------------------------- #
# Internals — tokenizer, quoting, escaping                                   #
# --------------------------------------------------------------------------- #


def _tokenize(query: str) -> list[_Token]:
    """Split *query* into terms, phrases, and prefix tokens.

    The grammar is deliberately simple and does not require a real parser:
    whitespace separates tokens, ``"..."`` produces a phrase, a trailing
    ``*`` on a bare word produces a prefix. Unmatched quote characters are
    swallowed (the remainder of the string becomes a phrase token) rather
    than rejected — a user typing a stray quote gets an approximate answer,
    not a hard error.

    Args:
        query: Raw user query.

    Returns:
        A list of tokens, in order. Empty tokens (e.g. from ``""``) are
        dropped.
    """
    tokens: list[_Token] = []
    i = 0
    n = len(query)
    while i < n:
        # Skip whitespace between tokens.
        while i < n and query[i].isspace():
            i += 1
        if i >= n:
            break

        if query[i] == '"':
            end = query.find('"', i + 1)
            if end == -1:
                phrase = query[i + 1 :].strip()
                i = n
            else:
                phrase = query[i + 1 : end]
                i = end + 1
            if phrase:
                tokens.append(_Token(kind="phrase", text=phrase))
            continue

        # Bare word, up to the next whitespace.
        end = i
        while end < n and not query[end].isspace():
            end += 1
        word = query[i:end]
        i = end

        if word.endswith("*"):
            stem = word.rstrip("*")
            if stem:
                tokens.append(_Token(kind="prefix", text=stem))
            # Empty stem (word was all asterisks) — ignore the token.
        else:
            tokens.append(_Token(kind="term", text=word))

    return tokens


def _quote_phrase(text: str) -> str:
    """Return *text* wrapped as an FTS5 phrase, with internal quotes doubled.

    FTS5 escapes a double quote inside a phrase by doubling it, exactly
    like SQL string literals. This function performs that escaping.

    Args:
        text: The phrase content, with no surrounding quotes.
    """
    return '"' + text.replace('"', '""') + '"'


def _escape_like(text: str) -> str:
    """Escape *text* for a LIKE clause using :data:`_LIKE_ESCAPE`.

    Escapes the escape character itself first so that a literal escape char
    in the input survives the subsequent wildcard escaping.

    Args:
        text: Unescaped substring to search for.
    """
    return (
        text.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", f"{_LIKE_ESCAPE}%")
        .replace("_", f"{_LIKE_ESCAPE}_")
    )


def _wrap_like(escaped: str) -> str:
    """Wrap an escaped substring in ``%`` markers for LIKE."""
    return f"%{escaped}%"


def _build_filter_clause(
    language: Language | None,
    status: MangaStatus | None,
    content_rating: ContentRating | None,
    *,
    table_alias: str,
) -> tuple[str, dict[str, Any]]:
    """Build the optional ``AND col = :param`` filter suffix for a query.

    Each filter that is set contributes one clause and one named parameter.
    The caller concatenates the returned SQL fragment directly into its
    ``WHERE`` expression; when no filters are set the fragment is empty
    (no trailing ``AND``).

    Args:
        language: Optional language enum filter.
        status: Optional status enum filter.
        content_rating: Optional content-rating enum filter.
        table_alias: Prefix for column references — ``"m"`` for FTS5
            joins, ``""`` for direct ``mangas`` queries.

    Returns:
        A ``(sql_fragment, params)`` tuple.
    """
    prefix = f"{table_alias}." if table_alias else ""
    fragments: list[str] = []
    params: dict[str, Any] = {}

    if language is not None:
        fragments.append(f"AND {prefix}language = :language")
        params["language"] = language.value
    if status is not None:
        fragments.append(f"AND {prefix}status = :status")
        params["status"] = status.value
    if content_rating is not None:
        fragments.append(f"AND {prefix}content_rating = :content_rating")
        params["content_rating"] = content_rating.value

    return (" ".join(fragments), params)


def _total_pages(total: int, per_page: int) -> int:
    """Return ``ceil(total / per_page)`` as an int; ``0`` when *total* is 0."""
    if total == 0:
        return 0
    return math.ceil(total / per_page)
