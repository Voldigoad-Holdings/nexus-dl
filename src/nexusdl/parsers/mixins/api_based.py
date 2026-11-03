"""Parser mixin for API-driven sites.

Several sites in this project expose a **public JSON API** rather than (or in
addition to) an HTML front-end: nhentai, Comick, MangaDex and Nekohouse.  For
those sites, the most reliable parser strategy is to talk to the API directly
— it gives structured metadata, reliable pagination, and predictable image
URLs, all without fighting Cloudflare interstitial pages that only exist on
the HTML side.

That said, **each API is different**.  nhentai returns
``{"result": [...]}``, Comick returns a bare list or ``{"result": [...]}``,
MangaDex returns a JSON:API envelope ``{"data": [...], "total": ...}``,
Nekohouse returns ``{"posts": [...]}``.  Envelopes, pagination parameters,
field names, error formats — every detail diverges.  There is **no universal
API contract** and this mixin does not try to invent one.

What this mixin provides
------------------------
A small set of helpers that concrete API parsers can reuse to reduce
boilerplate, while still implementing the four :class:`BaseParser` operations
(``search``, ``get_manga``, ``get_chapters``, ``get_pages``) themselves:

* :meth:`_fetch_json` — GET/POST JSON with normalised error handling.
* :meth:`_iter_json_pages` — iterate over a paginated JSON endpoint,
  following page- or offset-based pagination until the API stops returning
  results.
* :meth:`_extract_items` — recursively unwrap common envelope shapes
  (``data``, ``result``, ``items``, ``results``, ``posts``, ``chapters``)
  into a flat ``list[dict]``.
* :meth:`_coerce_int`, :meth:`_coerce_float` — defensive value coercions
  used everywhere in API parsing where a server may return ``None``,
  ``""``, ``"12"``, or ``12`` interchangeably.

Concrete parsers that opt to use these helpers will get:

* Uniform error messages that name the site and the offending URL, making
  log triage trivial.
* :class:`ParserParseError` for malformed JSON and
  :class:`ParserResponseError` for transport errors — instead of the
  generic exceptions that a raw ``session.get_json`` call might raise.
* A single pagination loop that is safe by construction (bounded, empty-batch
  aware, offset- or page-based).

Helpers this mixin deliberately does **not** provide
----------------------------------------------------
* A default ``search``, ``get_manga``, ``get_chapters`` or ``get_pages``.
  API contracts are too varied; every concrete parser must implement them.
* Query-string building.  ``httpx`` (and therefore :class:`HttpSession`)
  already handles list parameters, ``None`` values and encoding correctly
  through the ``params=`` argument.
* URL joining.  Callers use f-strings against ``self._base_url``; the API
  root is site-specific and belongs in the parser, not in a mixin.

Stacking with other mixins
--------------------------
``ApiBasedMixin`` composes cleanly with
:class:`~nexusdl.parsers.mixins.cloudflare.CloudflareMixin` for APIs that
require a Cloudflare cookie (Comick, MangaDex at times)::

    class MyParser(ApiBasedMixin, CloudflareMixin, BaseParser):
        ...

The API helpers go through ``session.get_json`` / ``session.post``, which
are **not** intercepted by :class:`CloudflareMixin` — Cloudflare handling
for those calls is already present at the ``HttpSession`` level through the
retry, circuit-breaker and cookie layers.  ``CloudflareMixin``'s
``session.get_html`` wrapper is therefore not needed and not used here;
only the ``get_html`` paths (HTML pages fetched by the parser itself, if
any) benefit from its interception, which is the correct behaviour.

Type information
----------------
The mixin relies on the following attributes provided by
:class:`~nexusdl.parsers.base.BaseParser`:

* ``site_id``, ``language``, ``adult`` — site identity.
* ``session`` — the shared :class:`HttpSession`.
* ``_log`` — the bound logger.

They are declared under :data:`TYPE_CHECKING` so mypy resolves
``self.<attr>`` inside this file without importing ``BaseParser`` at
runtime and without creating a circular import.
"""
from __future__ import annotations

import json
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncIterator,
    Callable,
    ClassVar,
)

from nexusdl.core.exceptions import (
    NexusDLError,
    ParserParseError,
    ParserResponseError,
)

if TYPE_CHECKING:
    from loguru import Logger

    from nexusdl.core.session.http_session import HttpSession


__all__ = ["ApiBasedMixin"]


#: Default envelope keys probed by :meth:`ApiBasedMixin._extract_items`.
#: Ordered by likelihood across the APIs in this project so that the most
#: common shape short-circuits the search.
_DEFAULT_ENVELOPE_KEYS: tuple[str, ...] = (
    "data",       # JSON:API — MangaDex
    "result",     # nhentai, Comick
    "items",      # generic
    "results",    # search APIs
    "posts",      # Nekohouse
    "chapters",   # Comick chapter lists
)


class ApiBasedMixin:
    """Mixin providing helpers for API-driven parsers.

    The four :class:`BaseParser` operations remain the responsibility of each
    concrete parser because API contracts differ too much to abstract.  This
    mixin exists so those concrete parsers share the boring parts.
    """

    if TYPE_CHECKING:
        site_id: str
        language: str
        adult: bool
        session: "HttpSession"
        _log: "Logger"

    #: Default page size used when the caller does not supply one.  APIs in
    #: this project either accept an explicit limit or return a fixed batch;
    #: 100 is a safe, widely supported value.
    _API_PAGE_SIZE: ClassVar[int] = 100

    #: Hard safety cap on the number of pages :meth:`_iter_json_pages` will
    #: follow.  Prevents an infinite loop on a broken API that keeps returning
    #: non-empty batches forever.
    _API_MAX_PAGES: ClassVar[int] = 1000

    # ------------------------------------------------------------------ #
    #  JSON fetching                                                     #
    # ------------------------------------------------------------------ #
    async def _fetch_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        method: str = "GET",
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        """Fetch and parse JSON from an API endpoint.

        Wraps :meth:`HttpSession.get_json` (for ``GET``) and
        :meth:`HttpSession.post` (for ``POST``), normalising every failure
        mode into the project's exception hierarchy:

        * :class:`ParserParseError` — the response body was not valid JSON.
        * :class:`ParserResponseError` — the request failed at the transport
          or HTTP level, or the response shape was unsupported.

        Non-JSON :class:`NexusDLError` subclasses raised by
        :class:`HttpSession` (e.g. :class:`SessionCloudflareError`) are
        propagated unchanged so the caller's Cloudflare handling — if any —
        still sees them.

        Args:
            url: Absolute API endpoint URL.
            params: Query parameters.  Handled by ``httpx``; lists become
                repeated query parameters.
            method: ``"GET"`` (default) or ``"POST"``.
            json_body: Request body for ``POST`` requests, serialised as
                JSON by ``httpx``.

        Returns:
            The decoded JSON value (``dict``, ``list``, or any scalar
            supported by JSON).

        Raises:
            ParserParseError: Invalid JSON in the response.
            ParserResponseError: Transport error, HTTP error, or unsupported
                response shape.
            NexusDLError: Any other :class:`NexusDLError` raised by the
                session layer, propagated unchanged.
        """
        try:
            if method == "GET":
                return await self.session.get_json(url, params=params)

            if method == "POST":
                post_kwargs: dict[str, Any] = {}
                if params is not None:
                    post_kwargs["params"] = params
                if json_body is not None:
                    post_kwargs["json"] = json_body
                response = await self.session.post(url, **post_kwargs)
                return self._parse_json_response(response, url)

            raise ParserResponseError(
                f"Unsupported HTTP method {method!r} for API call to "
                f"{url!r} on site {self.site_id!r}."
            )

        except NexusDLError:
            # Project-level errors (Cloudflare, circuit breaker, rate
            # limiter, timeout) are already meaningful — propagate them.
            raise
        except json.JSONDecodeError as exc:
            raise ParserParseError(
                f"Invalid JSON from {url!r} on site {self.site_id!r}: {exc}"
            ) from exc
        except Exception as exc:  # noqa: BLE001 — normalise third-party exceptions
            raise ParserResponseError(
                f"Failed to fetch JSON from {url!r} on site "
                f"{self.site_id!r}: {exc}"
            ) from exc

    @staticmethod
    def _parse_json_response(response: Any, url: str) -> Any:
        """Turn the value returned by ``session.post`` into parsed JSON.

        :class:`HttpSession.post` returns whatever the underlying transport
        returns — in current implementations that is either a ``str`` body
        or an object exposing ``.json()`` / ``.text``.  This helper
        normalises every shape into parsed JSON.

        Raises:
            ParserParseError: When the body is not valid JSON.
            ParserResponseError: When the response shape is unsupported.
        """
        if isinstance(response, (dict, list)):
            return response

        if isinstance(response, str):
            try:
                return json.loads(response)
            except json.JSONDecodeError as exc:
                raise ParserParseError(
                    f"Invalid JSON body from {url!r}: {exc}"
                ) from exc

        json_method = getattr(response, "json", None)
        if callable(json_method):
            try:
                return json_method()
            except json.JSONDecodeError as exc:
                raise ParserParseError(
                    f"Invalid JSON body from {url!r}: {exc}"
                ) from exc

        text = getattr(response, "text", None)
        if isinstance(text, str):
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise ParserParseError(
                    f"Invalid JSON body from {url!r}: {exc}"
                ) from exc

        raise ParserResponseError(
            f"Unsupported response type {type(response).__name__!r} for "
            f"JSON POST to {url!r}."
        )

    # ------------------------------------------------------------------ #
    #  Pagination                                                        #
    # ------------------------------------------------------------------ #
    async def _iter_json_pages(
        self,
        *,
        url: str,
        params: dict[str, Any] | None = None,
        page_param: str = "page",
        offset_param: str | None = None,
        per_page: int | None = None,
        start: int = 1,
        limit: int | None = None,
        extract: Callable[[Any], list[dict[str, Any]]] | None = None,
        stop_when_short: bool = True,
        max_pages: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Iterate over items from a paginated JSON endpoint.

        Follows a pagination loop that is safe by construction:

        1. Fetch the current page (page-based or offset-based).
        2. Extract the batch with *extract* (or
           :meth:`_extract_items` by default).
        3. If the batch is empty, stop.
        4. Yield each item.
        5. If ``stop_when_short`` is ``True`` and the batch size is below
           the effective page size, stop.
        6. If a ``limit`` is set and has been reached, stop.
        7. If the safety cap ``max_pages`` has been reached, stop.
        8. Increment the page number / offset and loop.

        Args:
            url: Absolute API endpoint URL.
            params: Base query parameters, copied and extended per page.
                Never mutated — the caller's dict is preserved.
            page_param: Name of the page-number parameter.  Used when
                ``offset_param`` is ``None``.
            offset_param: Name of the offset parameter.  When set, takes
                precedence over ``page_param`` and drives offset-based
                pagination.
            per_page: Number of items per page.  Used both as the value for
                the ``limit`` query parameter (when the API supports it) and
                as the threshold for ``stop_when_short``.  Defaults to
                :attr:`_API_PAGE_SIZE`.
            start: Starting page (page-based) or offset (offset-based).
                Defaults to 1 for page-based; for offset-based the caller
                should usually pass ``0``.
            limit: Maximum number of items to yield.  ``None`` means no
                limit (bounded only by the API and by ``max_pages``).
            extract: Callable that turns a decoded JSON value into a list of
                item dicts.  Defaults to :meth:`_extract_items`.
            stop_when_short: Stop when the batch size is below ``per_page``.
                Most APIs return full pages until the last one.  Set this to
                ``False`` for APIs that return partial pages throughout
                (rare, but possible).
            max_pages: Safety cap on the number of pages fetched.  Defaults
                to :attr:`_API_MAX_PAGES`.

        Yields:
            Individual items (``dict``) from every page, in API order.
        """
        effective_per_page = per_page if per_page is not None else self._API_PAGE_SIZE
        effective_max_pages = (
            max_pages if max_pages is not None else self._API_MAX_PAGES
        )
        effective_extract = extract if extract is not None else self._extract_items

        working_params: dict[str, Any] = dict(params or {})
        if "limit" not in working_params and "per_page" not in working_params:
            working_params.setdefault("limit", effective_per_page)

        cursor = start
        pages_fetched = 0
        items_yielded = 0

        while pages_fetched < effective_max_pages:
            if offset_param is not None:
                working_params[offset_param] = cursor
            else:
                working_params[page_param] = cursor

            payload = await self._fetch_json(url, params=working_params)
            batch = effective_extract(payload)

            if not batch:
                return

            for item in batch:
                yield item
                items_yielded += 1
                if limit is not None and items_yielded >= limit:
                    return

            if stop_when_short and len(batch) < effective_per_page:
                return

            if offset_param is not None:
                cursor += effective_per_page
            else:
                cursor += 1

            pages_fetched += 1

        self._log.debug(
            "Pagination safety cap ({}) reached for API call to {} "
            "on site {} after {} items.",
            effective_max_pages,
            url,
            self.site_id,
            items_yielded,
        )

    # ------------------------------------------------------------------ #
    #  Envelope unwrapping                                               #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_items(
        payload: Any,
        *,
        keys: tuple[str, ...] = _DEFAULT_ENVELOPE_KEYS,
    ) -> list[dict[str, Any]]:
        """Unwrap a JSON envelope into a flat list of item dicts.

        Handles every common shape used by the APIs in this project:

        * A bare ``list`` — returned as-is, filtered to dict items.
        * A ``dict`` with one of *keys* at the top level — recursively
          unwrapped (so ``{data: {items: [...]}}`` works too).
        * A ``dict`` without any of *keys* — treated as a single-item
          response and wrapped in a one-element list **only when** the
          dict is non-empty; an empty dict yields an empty list.
        * ``None`` or any other type — yields an empty list.

        Args:
            payload: The decoded JSON value.
            keys: Envelope keys to probe, in priority order.

        Returns:
            A list of item dicts.  Never ``None``.
        """
        if payload is None:
            return []

        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]

        if isinstance(payload, dict):
            for key in keys:
                if key in payload:
                    return ApiBasedMixin._extract_items(
                        payload[key], keys=keys
                    )
            return [payload] if payload else []

        return []

    # ------------------------------------------------------------------ #
    #  Value coercion                                                    #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _coerce_int(value: Any, default: int | None = None) -> int | None:
        """Coerce *value* to ``int``, returning *default* on failure.

        API fields that are "supposed" to be integers are frequently
        delivered as strings (``"12"``), floats (``12.0``), or ``None``.
        This helper normalises them without the boilerplate of a
        ``try``/``except`` block at every call site.

        Returns *default* when *value* is ``None``, empty, or cannot be
        coerced.  Integer-valued floats are accepted (``12.0`` → ``12``);
        non-integral floats yield *default* (``12.5`` → *default*).
        """
        if value is None:
            return default
        if isinstance(value, bool):
            # ``bool`` is an ``int`` subclass in Python, but treating
            # ``True`` as ``1`` in an API context is rarely intended.
            return default
        try:
            if isinstance(value, float):
                if not value.is_integer():
                    return default
                return int(value)
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _coerce_float(value: Any, default: float | None = None) -> float | None:
        """Coerce *value* to ``float``, returning *default* on failure.

        Same rationale as :meth:`_coerce_int`.  Accepts integers, numeric
        strings, and integral or non-integral floats.  Returns *default*
        for ``None``, empty strings, and anything that cannot be parsed.
        """
        if value is None:
            return default
        if isinstance(value, bool):
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
