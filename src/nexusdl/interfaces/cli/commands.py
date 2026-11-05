"""All NexusDL CLI sub-command groups.

This module defines the seven :class:`typer.Typer` sub-applications that
:mod:`nexusdl.interfaces.cli.cli_entry` registers on the root ``nexusdl``
command:

* :data:`search_app`  — search mangas across every enabled site.
* :data:`download_app` — download one manga from a source site.
* :data:`library_app` — inspect and manage the local library.
* :data:`config_app`  — inspect the loaded configuration.
* :data:`sites_app`   — inspect the source-site registry.
* :data:`plugins_app` — inspect and validate plugins.
* :data:`admin_app`   — administrative operations (``init``, ``clean``).

Every command follows the same three-rule contract:

1. **Retrieve the state, then the services.** A command does
   ``state: CLIState = ctx.obj`` followed by ``wired = state.ensure()``.
   ``state.ensure()`` bootstraps every service on first call and
   re-raises the same ``typer.Exit`` on every subsequent call if the
   bootstrap failed. ``CLIState`` is imported **under**
   :data:`TYPE_CHECKING` only, so this module and ``cli_entry.py`` do
   not form a runtime import cycle.

2. **Wrap the whole command body in** ``with _handle_errors():``. The
   context manager converts any :class:`~nexusdl.core.exceptions.NexusDLError`
   into a clean ``Error: …`` line on stderr and exit code 1; it lets a
   :class:`typer.Exit` raised by the command itself propagate unchanged;
   and it converts ``KeyboardInterrupt`` into the conventional exit code
   130.

3. **Print structured output through** :class:`rich.console.Console` —
   never a bare :func:`print`. Every list-producing command supports a
   ``--json`` flag; when set, the command emits one JSON document on
   stdout and no other output.

Synchronous vs. asynchronous commands
-------------------------------------
Commands that need the wired services must be declared **synchronous**
(``def``), not ``async def``. :meth:`CLIState.ensure` internally creates
a fresh :class:`asyncio` event loop to run the service initializers;
calling it from inside an already-running loop would raise
``RuntimeError: Cannot run the event loop while another loop is running``.

The pattern is: ``def`` command, ``asyncio.run(...)`` wrapping the async
body.

Commands that need **no** bootstrap (``admin init``) are also ``def``,
but skip ``_services(ctx)`` entirely — they work before ``.env`` exists.

Deferred imports
----------------
:mod:`nexusdl.parsers.factory` (imported transitively by
``SiteRegistry.initialize``), :mod:`nexusdl.core.library.scanner`, and
the :mod:`importlib` machinery used by ``sites check-parsers`` are all
imported **inside** the commands that need them.

Deterministic output
--------------------
Every list command sorts its output deterministically (alphabetical for
sites and plugins, most-recent-first for library listings, arrival
order for search results).
"""

from __future__ import annotations

import asyncio
import importlib
import json
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
from rich.console import Console
from rich.table import Table
from rich.tree import Tree

from nexusdl.core.constants import APP_NAME, APP_SLUG
from nexusdl.core.exceptions import NexusDLError
from nexusdl.core.models.enums import Language, PackagingFormat
from nexusdl.core.utils.filesystem import human_readable_size

if TYPE_CHECKING:
    from nexusdl.core.models.chapter import Chapter
    from nexusdl.core.models.manga import Manga
    from nexusdl.core.models.search import SearchResult
    from nexusdl.interfaces.cli.cli_entry import CLIState, WiredServices


__all__ = [
    "admin_app",
    "config_app",
    "download_app",
    "library_app",
    "plugins_app",
    "search_app",
    "sites_app",
]


# --------------------------------------------------------------------------- #
# Shared constants and consoles                                              #
# --------------------------------------------------------------------------- #

_EXIT_ERROR: int = 1
_EXIT_KEYBOARD_INTERRUPT: int = 130

_out_console = Console()
_err_console = Console(stderr=True)


# --------------------------------------------------------------------------- #
# Shared helpers                                                             #
# --------------------------------------------------------------------------- #


def _services(ctx: typer.Context) -> WiredServices:
    """Return the wired services, bootstrapping on first call."""
    state: CLIState = ctx.obj
    return state.ensure()


@contextmanager
def _handle_errors() -> Iterator[None]:
    """Convert pipeline exceptions into clean CLI exits."""
    try:
        yield
    except typer.Exit:
        raise
    except NexusDLError as exc:
        _err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=_EXIT_ERROR) from exc
    except KeyboardInterrupt:
        _err_console.print("[yellow]Interrupted.[/yellow]")
        raise typer.Exit(code=_EXIT_KEYBOARD_INTERRUPT) from None


def _echo_json(payload: Any) -> None:
    """Print *payload* as one pretty-printed JSON document on stdout."""
    typer.echo(json.dumps(payload, indent=2, default=str))


def _parse_format(raw: str | None) -> PackagingFormat | None:
    """Parse a ``--format`` argument into a :class:`PackagingFormat`."""
    if raw is None:
        return None
    try:
        return PackagingFormat(raw.lower())
    except ValueError as exc:
        _err_console.print(
            f"[red]Unknown format:[/red] {raw!r}. "
            f"Supported: cbz, cb7, zip, pdf, folder."
        )
        raise typer.Exit(_EXIT_ERROR) from exc


def _parse_language(raw: str | None) -> Language | None:
    """Parse a ``--language`` argument into a :class:`Language`."""
    if raw is None:
        return None
    try:
        return Language(raw.upper())
    except ValueError as exc:
        _err_console.print(
            f"[red]Unknown language:[/red] {raw!r}"
        )
        raise typer.Exit(_EXIT_ERROR) from exc


# --------------------------------------------------------------------------- #
# search                                                                     #
# --------------------------------------------------------------------------- #

search_app = typer.Typer(
    name="search",
    help="Search for mangas across every enabled source site.",
    no_args_is_help=True,
)


@search_app.command("run")
def search_run(
    ctx: typer.Context,
    query: Annotated[str, typer.Argument(help="The search query.")],
    site: Annotated[
        str | None,
        typer.Option(
            "--site", "-s",
            help="Restrict the search to a single site id.",
        ),
    ] = None,
    language: Annotated[
        str | None,
        typer.Option(
            "--language", "-l",
            help="Restrict results to one content language (en, fr, …).",
        ),
    ] = None,
    adult: Annotated[
        bool,
        typer.Option(
            "--adult/--no-adult",
            help="Include adult sites in the fan-out.",
        ),
    ] = False,
    limit: Annotated[
        int,
        typer.Option(
            "--limit", "-n",
            min=1, max=200,
            help="Maximum number of results to print.",
        ),
    ] = 50,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit machine-readable JSON instead of a table.",
        ),
    ] = False,
) -> None:
    """Search every enabled site for QUERY and print the results."""
    with _handle_errors():
        wired = _services(ctx)
        asyncio.run(
            _search_run_async(
                wired,
                query,
                site_id=site,
                language=language,
                adult=adult,
                limit=limit,
                json_output=json_output,
            )
        )


async def _search_run_async(
    wired: WiredServices,
    query: str,
    *,
    site_id: str | None,
    language: str | None,
    adult: bool,
    limit: int,
    json_output: bool,
) -> None:
    """Async body of ``nexusdl search run``."""
    if site_id is not None and wired.registry.get_site(site_id) is None:
        _err_console.print(
            f"[red]Unknown site:[/red] {site_id!r}. "
            f"Run `nexusdl sites list` to see the registered sites."
        )
        raise typer.Exit(_EXIT_ERROR)

    lang_filter = _parse_language(language)
    results: list[SearchResult] = []
    sites_queried: set[str] = set()
    started = datetime.now(UTC)

    if not json_output:
        _out_console.print(
            f"Searching for [bold]{query}[/bold]…", highlight=False
        )

    async for result in wired.registry.search_all(
        query,
        session=wired.session,
        languages=[lang_filter] if lang_filter is not None else None,
        include_adult=adult,
    ):
        if site_id is not None and result.site_id != site_id:
            continue
        sites_queried.add(result.site_id)
        results.append(result)
        if not json_output:
            _out_console.print(
                _format_search_line(result), highlight=False
            )
        if len(results) >= limit:
            break

    elapsed = (datetime.now(UTC) - started).total_seconds()

    if json_output:
        _echo_json(
            {
                "query": query,
                "sites_queried": sorted(sites_queried),
                "count": len(results),
                "elapsed_seconds": round(elapsed, 2),
                "results": [_serialize_search_result(r) for r in results],
            }
        )
        return

    _out_console.print()
    _out_console.print(
        f"[bold]{len(results)} result(s)[/bold] "
        f"from {len(sites_queried)} site(s) "
        f"in {elapsed:.1f}s",
        highlight=False,
    )


def _format_search_line(result: SearchResult) -> str:
    """Render one search result as a single terminal line."""
    lang = result.language.value if result.language else "?"
    author = f"  by {result.author}" if result.author else ""
    return (
        f"[cyan]{result.site_id}[/cyan]  "
        f"{result.title}"
        f"[dim] ({lang})[/dim]"
        f"{author}"
        f"  [dim]{result.manga_id}[/dim]"
    )


def _serialize_search_result(result: SearchResult) -> dict[str, Any]:
    """Serialize a :class:`SearchResult` for ``--json`` output."""
    return {
        "manga_id": result.manga_id,
        "site_id": result.site_id,
        "source_id": result.source_id,
        "title": result.title,
        "url": result.url,
        "cover_url": result.cover_url,
        "author": result.author,
        "description": result.description,
        "language": result.language.value if result.language else None,
        "year": result.year,
        "status": result.status.value if result.status else None,
        "content_rating": (
            result.content_rating.value
            if result.content_rating
            else None
        ),
    }


# --------------------------------------------------------------------------- #
# download                                                                   #
# --------------------------------------------------------------------------- #

download_app = typer.Typer(
    name="download",
    help="Download a manga from a source site.",
    no_args_is_help=True,
)


@download_app.command("manga")
def download_manga_cmd(
    ctx: typer.Context,
    url_or_id: Annotated[
        str,
        typer.Argument(
            help="Full URL of the manga on the source site, or the "
            "site-specific id (requires --site)."
        ),
    ],
    site: Annotated[
        str | None,
        typer.Option(
            "--site", "-s",
            help=(
                "Source site id. Required when url_or_id is a bare id; "
                "auto-detected from the URL's host otherwise."
            ),
        ),
    ] = None,
    chapter: Annotated[
        list[str] | None,
        typer.Option(
            "--chapter", "-c",
            help=(
                "Download only the given chapter number(s). "
                "Repeatable. Omit to download every chapter."
            ),
        ),
    ] = None,
    format_: Annotated[
        str | None,
        typer.Option(
            "--format", "-f",
            help="Packaging format: cbz, cb7, zip, pdf, folder.",
        ),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output", "-o",
            help="Override the output directory (defaults to the "
            "downloads directory from settings).",
        ),
    ] = None,
    skip_existing: Annotated[
        bool,
        typer.Option(
            "--skip-existing/--force",
            help=(
                "Skip chapters whose output file already exists "
                "(default), or force re-download."
            ),
        ),
    ] = True,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes", "-y",
            help="Skip the confirmation prompt before starting.",
        ),
    ] = False,
) -> None:
    """Download every (or only the selected) chapter(s) of a manga."""
    with _handle_errors():
        wired = _services(ctx)
        asyncio.run(
            _download_manga_async(
                wired,
                url_or_id,
                site_id=site,
                chapters=chapter,
                format_raw=format_,
                output=output,
                skip_existing=skip_existing,
                yes=yes,
            )
        )


async def _download_manga_async(
    wired: WiredServices,
    url_or_id: str,
    *,
    site_id: str | None,
    chapters: list[str] | None,
    format_raw: str | None,
    output: Path | None,
    skip_existing: bool,
    yes: bool,
) -> None:
    """Async body of ``nexusdl download manga``."""
    from nexusdl.core.utils.url import get_host

    # --- Resolve the site ------------------------------------------------ #
    if site_id is None:
        host = get_host(url_or_id)
        if host:
            for site in wired.registry.list_sites(
                include_adult=True, include_disabled=True
            ):
                if any(host in d for d in site.domains):
                    site_id = site.id
                    break
        if site_id is None:
            _err_console.print(
                "[red]Cannot determine the source site.[/red] "
                "Pass --site SITE_ID explicitly. "
                "Run `nexusdl sites list` to see the registered sites."
            )
            raise typer.Exit(_EXIT_ERROR)

    if wired.registry.get_site(site_id) is None:
        _err_console.print(
            f"[red]Unknown site:[/red] {site_id!r}"
        )
        raise typer.Exit(_EXIT_ERROR)

    packaging_format = _parse_format(format_raw) or (
        wired.settings.downloader.default_format
    )

    # --- Fetch the manga ------------------------------------------------- #
    _out_console.print(f"Resolving {url_or_id!r} on {site_id!r}…")
    parser = wired.registry.get_parser(site_id, wired.session)
    manga = await parser.get_manga(url_or_id)
    _out_console.print(
        f"[bold]{manga.title}[/bold] "
        f"[dim]({manga.language.value}, {manga.status.value})[/dim]"
    )

    # --- Fetch the chapters --------------------------------------------- #
    all_chapters = await parser.get_chapters(manga)
    if not all_chapters:
        _err_console.print(
            "[yellow]The parser returned no chapters.[/yellow]"
        )
        raise typer.Exit(_EXIT_ERROR)

    if chapters:
        wanted = set(chapters)
        selected = [c for c in all_chapters if c.number in wanted]
        if not selected:
            _err_console.print(
                f"[red]No matching chapters for {sorted(wanted)}.[/red]"
            )
            raise typer.Exit(_EXIT_ERROR)
    else:
        selected = all_chapters

    # --- Confirm --------------------------------------------------------- #
    if not yes:
        _out_console.print(
            f"About to download [bold]{len(selected)}[/bold] chapter(s) "
            f"as [bold]{packaging_format.value}[/bold] "
            f"to [bold]{output or wired.settings.storage.downloads_dir}[/bold]."
        )
        confirmed = typer.confirm("Continue?", default=True)
        if not confirmed:
            _out_console.print("[yellow]Cancelled.[/yellow]")
            raise typer.Exit(0)

    # --- Download -------------------------------------------------------- #
    started = datetime.now(UTC)
    succeeded = 0
    failed = 0
    async for result in wired.manager.download_manga(
        manga,
        chapters=selected,
        format=packaging_format,
        skip_existing=skip_existing,
    ):
        if result.status.value == "DONE":
            succeeded += 1
            _out_console.print(
                f"[green]✓[/green] {result.output_path}",
                highlight=False,
            )
        else:
            failed += 1
            _err_console.print(
                f"[red]✗[/red] {result.status.value}: {result.error}"
            )

    elapsed = (datetime.now(UTC) - started).total_seconds()
    _out_console.print()
    _out_console.print(
        f"[bold]Downloaded[/bold] {succeeded} chapter(s)"
        + (f", [red]{failed} failed[/red]" if failed else "")
        + f" in {elapsed:.1f}s",
        highlight=False,
    )
    if failed:
        raise typer.Exit(_EXIT_ERROR)


# --------------------------------------------------------------------------- #
# library                                                                    #
# --------------------------------------------------------------------------- #

library_app = typer.Typer(
    name="library",
    help="Inspect and manage the local library.",
    no_args_is_help=True,
)


@library_app.command("list")
def library_list(
    ctx: typer.Context,
    page: Annotated[
        int, typer.Option("--page", "-p", min=1, help="Page number.")
    ] = 1,
    per_page: Annotated[
        int,
        typer.Option(
            "--per-page", "-n", min=1, max=200, help="Page size."
        ),
    ] = 20,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON.")
    ] = False,
) -> None:
    """List the mangas in the local library, most-recent first."""
    with _handle_errors():
        wired = _services(ctx)
        asyncio.run(
            _library_list_async(
                wired,
                page=page,
                per_page=per_page,
                json_output=json_output,
            )
        )


async def _library_list_async(
    wired: WiredServices,
    *,
    page: int,
    per_page: int,
    json_output: bool,
) -> None:
    """Async body of ``nexusdl library list``."""
    result = await wired.database.list_mangas(
        page=page, per_page=per_page
    )

    if json_output:
        _echo_json(
            {
                "page": result.page,
                "per_page": result.per_page,
                "total": result.total,
                "total_pages": result.total_pages,
                "items": [_serialize_manga(m) for m in result.items],
            }
        )
        return

    if not result.items:
        _out_console.print(
            "[yellow]The library is empty.[/yellow] "
            "Run `nexusdl library scan <directory>` to import archives."
        )
        return

    table = Table(title=f"Library — page {result.page}/{result.total_pages}")
    table.add_column("ID")
    table.add_column("Title")
    table.add_column("Lang")
    table.add_column("Status")
    table.add_column("Chapters", justify="right")
    table.add_column("Last read")
    for manga in result.items:
        table.add_row(
            manga.id,
            manga.title,
            manga.language.value,
            manga.status.value,
            str(manga.chapter_count),
            manga.last_read_at.strftime("%Y-%m-%d")
            if manga.last_read_at
            else "—",
        )
    _out_console.print(table)


@library_app.command("show")
def library_show(
    ctx: typer.Context,
    manga_id: Annotated[str, typer.Argument(help="The manga's library id.")],
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON.")
    ] = False,
) -> None:
    """Show the full stored metadata of a single manga."""
    with _handle_errors():
        wired = _services(ctx)
        asyncio.run(
            _library_show_async(
                wired, manga_id, json_output=json_output
            )
        )


async def _library_show_async(
    wired: WiredServices,
    manga_id: str,
    *,
    json_output: bool,
) -> None:
    manga = await wired.database.get_manga(manga_id)
    if manga is None:
        _err_console.print(
            f"[red]Manga not found:[/red] {manga_id!r}"
        )
        raise typer.Exit(_EXIT_ERROR)

    if json_output:
        _echo_json(_serialize_manga(manga))
        return

    _out_console.print(
        f"[bold]{manga.title}[/bold]  "
        f"[dim]({manga.language.value}, {manga.status.value})[/dim]"
    )
    for label, value in (
        ("id", manga.id),
        ("site_id", manga.site_id),
        ("source_id", manga.source_id),
        ("author", manga.author or "—"),
        ("artist", manga.artist or "—"),
        ("year", str(manga.year) if manga.year else "—"),
        ("chapters", str(manga.chapter_count)),
        ("pages", str(manga.page_count)),
        ("url", manga.url),
    ):
        _out_console.print(f"  [cyan]{label:<12}[/cyan] {value}")


@library_app.command("chapters")
def library_chapters(
    ctx: typer.Context,
    manga_id: Annotated[str, typer.Argument(help="The manga's library id.")],
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON.")
    ] = False,
) -> None:
    """List the stored chapters of a single manga, in reading order."""
    with _handle_errors():
        wired = _services(ctx)
        asyncio.run(
            _library_chapters_async(
                wired, manga_id, json_output=json_output
            )
        )


async def _library_chapters_async(
    wired: WiredServices,
    manga_id: str,
    *,
    json_output: bool,
) -> None:
    chapters = await wired.database.get_chapters(manga_id)
    if not chapters:
        _out_console.print(
            "[yellow]No chapters stored for this manga.[/yellow]"
        )
        return

    if json_output:
        _echo_json([_serialize_chapter(c) for c in chapters])
        return

    table = Table(title=f"Chapters of {manga_id}")
    table.add_column("Number")
    table.add_column("Title")
    table.add_column("Pages", justify="right")
    table.add_column("Language")
    for chapter in chapters:
        table.add_row(
            chapter.number or "—",
            chapter.title,
            str(chapter.page_count),
            chapter.language.value,
        )
    _out_console.print(table)


@library_app.command("search")
def library_search(
    ctx: typer.Context,
    query: Annotated[str, typer.Argument(help="The search query.")],
    limit: Annotated[
        int,
        typer.Option("--limit", "-n", min=1, max=200, help="Max results."),
    ] = 20,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON.")
    ] = False,
) -> None:
    """Full-text search the local library."""
    with _handle_errors():
        wired = _services(ctx)
        asyncio.run(
            _library_search_async(
                wired, query, limit=limit, json_output=json_output
            )
        )


async def _library_search_async(
    wired: WiredServices,
    query: str,
    *,
    limit: int,
    json_output: bool,
) -> None:
    result = await wired.database.search(query, limit=limit)
    if json_output:
        _echo_json(
            {
                "query": query,
                "count": result.total,
                "items": [_serialize_manga(m) for m in result.items],
            }
        )
        return

    if not result.items:
        _out_console.print(
            f"[yellow]No mangas match {query!r}.[/yellow]"
        )
        return

    table = Table(title=f"Search results for {query!r}")
    table.add_column("ID")
    table.add_column("Title")
    table.add_column("Lang")
    for manga in result.items:
        table.add_row(manga.id, manga.title, manga.language.value)
    _out_console.print(table)


@library_app.command("scan")
def library_scan(
    ctx: typer.Context,
    root: Annotated[
        Path, typer.Argument(help="Directory to scan for archives.")
    ],
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON.")
    ] = False,
) -> None:
    """Recursively scan a directory and import every CBZ/CB7/CBT archive."""
    with _handle_errors():
        wired = _services(ctx)
        asyncio.run(
            _library_scan_async(wired, root, json_output=json_output)
        )


async def _library_scan_async(
    wired: WiredServices, root: Path, *, json_output: bool
) -> None:
    from nexusdl.core.library.scanner import LibraryScanner

    if not root.is_dir():
        _err_console.print(
            f"[red]Not a directory:[/red] {root}"
        )
        raise typer.Exit(_EXIT_ERROR)

    scanner = LibraryScanner(repository=wired.database)
    report = await scanner.scan(root)

    if json_output:
        _echo_json(
            {
                "root": str(report.root),
                "files_found": report.files_found,
                "files_imported": report.files_imported,
                "files_failed": report.files_failed,
                "mangas_imported": report.mangas_imported,
                "elapsed_seconds": round(report.elapsed_seconds, 2),
                "failures": [
                    {
                        "path": str(f.archive_path),
                        "error_type": f.error_type,
                        "message": f.error_message,
                    }
                    for f in report.failures
                ],
            }
        )
        return

    _out_console.print(
        f"Imported [bold]{report.files_imported}[/bold] of "
        f"[bold]{report.files_found}[/bold] file(s) as "
        f"[bold]{report.mangas_imported}[/bold] manga(s) "
        f"in {report.elapsed_seconds:.1f}s"
    )
    if report.failures:
        _err_console.print(
            f"[yellow]{len(report.failures)} file(s) failed:[/yellow]"
        )
        for failure in report.failures:
            _err_console.print(
                f"  [red]✗[/red] {failure.archive_path}: "
                f"{failure.error_message}"
            )


@library_app.command("stats")
def library_stats(
    ctx: typer.Context,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON.")
    ] = False,
) -> None:
    """Show aggregate counts for the local library."""
    with _handle_errors():
        wired = _services(ctx)
        asyncio.run(_library_stats_async(wired, json_output=json_output))


async def _library_stats_async(
    wired: WiredServices, *, json_output: bool
) -> None:
    from sqlalchemy import text as _text

    async with wired.database.session() as sess:
        manga_count = (
            await sess.execute(_text("SELECT COUNT(*) FROM mangas"))
        ).scalar_one()
        chapter_count = (
            await sess.execute(_text("SELECT COUNT(*) FROM chapters"))
        ).scalar_one()
        page_count = (
            await sess.execute(
                _text("SELECT COALESCE(SUM(page_count), 0) FROM chapters")
            )
        ).scalar_one()

    payload = {
        "manga_count": int(manga_count),
        "chapter_count": int(chapter_count),
        "page_count": int(page_count),
    }
    if json_output:
        _echo_json(payload)
        return
    _out_console.print(
        f"[bold]{payload['manga_count']}[/bold] mangas, "
        f"[bold]{payload['chapter_count']}[/bold] chapters, "
        f"[bold]{payload['page_count']}[/bold] pages"
    )


@library_app.command("delete")
def library_delete(
    ctx: typer.Context,
    manga_id: Annotated[str, typer.Argument(help="The manga's library id.")],
    delete_files: Annotated[
        bool,
        typer.Option(
            "--delete-files/--keep-files",
            help=(
                "Also delete the manga's archive files from disk. "
                "Defaults to keeping them."
            ),
        ),
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")
    ] = False,
) -> None:
    """Remove a manga from the local library."""
    with _handle_errors():
        wired = _services(ctx)
        asyncio.run(
            _library_delete_async(
                wired,
                manga_id,
                delete_files=delete_files,
                yes=yes,
            )
        )


async def _library_delete_async(
    wired: WiredServices,
    manga_id: str,
    *,
    delete_files: bool,
    yes: bool,
) -> None:
    manga = await wired.database.get_manga(manga_id)
    if manga is None:
        _err_console.print(
            f"[red]Manga not found:[/red] {manga_id!r}"
        )
        raise typer.Exit(_EXIT_ERROR)

    if not yes:
        suffix = " (and its archive files)" if delete_files else ""
        _out_console.print(
            f"About to delete [bold]{manga.title}[/bold] "
            f"({manga.id}){suffix}."
        )
        confirmed = typer.confirm("Continue?", default=False)
        if not confirmed:
            _out_console.print("[yellow]Cancelled.[/yellow]")
            raise typer.Exit(0)

    deleted = await wired.database.delete_manga(
        manga_id, delete_files=delete_files
    )
    if deleted:
        _out_console.print(f"[green]Deleted[/green] {manga_id}")
    else:
        _err_console.print(
            f"[yellow]Nothing to delete for {manga_id!r}.[/yellow]"
        )
        raise typer.Exit(_EXIT_ERROR)


# --------------------------------------------------------------------------- #
# config                                                                     #
# --------------------------------------------------------------------------- #

config_app = typer.Typer(
    name="config",
    help="Inspect the loaded configuration.",
    no_args_is_help=True,
)


@config_app.command("show")
def config_show(
    ctx: typer.Context,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON.")
    ] = False,
) -> None:
    """Print the effective settings, with secrets masked."""
    with _handle_errors():
        wired = _services(ctx)
        settings = wired.settings

        if json_output:
            _echo_json(_masked_settings(settings))
            return

        _print_masked_settings(settings)


@config_app.command("paths")
def config_paths(
    ctx: typer.Context,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON.")
    ] = False,
) -> None:
    """Print every filesystem path the pipeline uses, with existence markers."""
    with _handle_errors():
        wired = _services(ctx)
        from nexusdl.core.paths import describe as describe_paths

        paths = describe_paths(wired.settings)

        if json_output:
            _echo_json(
                {
                    name: {
                        "path": str(p),
                        "exists": Path(p).exists(),
                        "is_dir": Path(p).is_dir(),
                        "is_file": Path(p).is_file(),
                    }
                    for name, p in paths.items()
                }
            )
            return

        table = Table(title="NexusDL paths")
        table.add_column("Name")
        table.add_column("Path")
        table.add_column("Status")
        for name, p in paths.items():
            path = Path(p)
            if path.is_file():
                status = (
                    f"[green]file[/green] "
                    f"({human_readable_size(path.stat().st_size)})"
                )
            elif path.is_dir():
                status = "[green]directory[/green]"
            else:
                status = "[yellow]missing[/yellow]"
            table.add_row(name, str(path), status)
        _out_console.print(table)


@config_app.command("validate")
def config_validate(
    ctx: typer.Context,
) -> None:
    """Load the configuration and report any problem."""
    with _handle_errors():
        wired = _services(ctx)
        _out_console.print(
            f"[green]✓[/green] Configuration loaded: "
            f"{APP_NAME} "
            f"(debug={wired.settings.app.debug})"
        )


# --------------------------------------------------------------------------- #
# sites                                                                      #
# --------------------------------------------------------------------------- #

sites_app = typer.Typer(
    name="sites",
    help="Inspect the source-site registry.",
    no_args_is_help=True,
)


@sites_app.command("list")
def sites_list(
    ctx: typer.Context,
    include_adult: Annotated[
        bool, typer.Option("--adult", help="Include adult sites.")
    ] = False,
    include_disabled: Annotated[
        bool, typer.Option("--disabled", help="Include disabled sites.")
    ] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON.")
    ] = False,
) -> None:
    """List every registered source site."""
    with _handle_errors():
        wired = _services(ctx)
        sites = wired.registry.list_sites(
            include_adult=include_adult,
            include_disabled=include_disabled,
        )
        if json_output:
            _echo_json(
                [
                    {
                        "id": s.id,
                        "name": s.name,
                        "language": s.language.value,
                        "adult": s.adult,
                        "capabilities": {
                            k: getattr(s.capabilities, k)
                            for k in s.capabilities.__slots__
                        },
                    }
                    for s in sites
                ]
            )
            return

        if not sites:
            _out_console.print("[yellow]No sites matched.[/yellow]")
            return

        table = Table(title=f"{len(sites)} site(s)")
        table.add_column("ID")
        table.add_column("Name")
        table.add_column("Lang")
        table.add_column("Adult")
        table.add_column("Capabilities")
        for site in sites:
            caps = ", ".join(site.capabilities.enabled())
            table.add_row(
                site.id,
                site.name,
                site.language.value,
                "yes" if site.adult else "—",
                caps or "—",
            )
        _out_console.print(table)


@sites_app.command("show")
def sites_show(
    ctx: typer.Context,
    site_id: Annotated[str, typer.Argument(help="The site id.")],
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON.")
    ] = False,
) -> None:
    """Show the full configuration of a single site."""
    with _handle_errors():
        wired = _services(ctx)
        site = wired.registry.get_site(site_id)
        if site is None:
            _err_console.print(f"[red]Unknown site:[/red] {site_id!r}")
            raise typer.Exit(_EXIT_ERROR)

        if json_output:
            _echo_json(
                {
                    "id": site.id,
                    "name": site.name,
                    "domains": list(site.domains),
                    "parser_class": site.parser_class,
                    "language": site.language.value,
                    "adult": site.adult,
                    "capabilities": {
                        k: getattr(site.capabilities, k)
                        for k in site.capabilities.__slots__
                    },
                    "cookies_required": list(site.cookies_required),
                    "max_concurrent_downloads": site.max_concurrent_downloads,
                    "rate_limit": site.rate_limit,
                }
            )
            return

        _out_console.print(f"[bold]{site.name}[/bold]  ({site.id})")
        for label, value in (
            ("Domains", ", ".join(site.domains)),
            ("Parser class", site.parser_class),
            ("Language", site.language.value),
            ("Adult", "yes" if site.adult else "no"),
            ("Rate limit", f"{site.rate_limit}/s"),
            ("Max concurrency", str(site.max_concurrent_downloads)),
            ("Cookies required", ", ".join(site.cookies_required) or "—"),
            (
                "Capabilities",
                ", ".join(site.capabilities.enabled()) or "—",
            ),
        ):
            _out_console.print(
                f"  [cyan]{label:<20}[/cyan] {value}"
            )


@sites_app.command("validate")
def sites_validate(
    ctx: typer.Context,
) -> None:
    """Validate ``sites.yaml`` against its JSON schema and every site's model."""
    with _handle_errors():
        wired = _services(ctx)
        sites = wired.registry.list_sites(
            include_adult=True, include_disabled=True
        )
        _out_console.print(
            f"[green]✓[/green] {len(sites)} site(s) validated."
        )


@sites_app.command("check-parsers")
def sites_check_parsers(
    ctx: typer.Context,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON.")
    ] = False,
) -> None:
    """Verify every ``parser_class`` resolves to an importable class."""
    with _handle_errors():
        wired = _services(ctx)
        from nexusdl.core.registry.loader import (
            check_parser_classes,
            load_and_validate_sites,
        )

        def _resolve(parser_class: str) -> str | None:
            try:
                module_path, class_name = parser_class.split(":", 1)
                module = importlib.import_module(module_path)
                getattr(module, class_name)
            except (ImportError, AttributeError, ValueError) as exc:
                return f"{type(exc).__name__}: {exc}"
            return None

        document = load_and_validate_sites()
        issues = check_parser_classes(document, _resolve)

        if json_output:
            _echo_json(
                {
                    "checked": len(document.get("sites", [])),
                    "issues": [
                        {
                            "index": i.index,
                            "parser_class": i.parser_class,
                            "reason": i.reason,
                        }
                        for i in issues
                    ],
                }
            )
            if issues:
                raise typer.Exit(_EXIT_ERROR)
            return

        if not issues:
            _out_console.print(
                "[green]✓[/green] Every parser class resolves."
            )
            return
        _err_console.print(
            f"[red]{len(issues)} parser class(es) do not resolve:[/red]"
        )
        for issue in issues:
            _err_console.print(f"  {issue}")
        raise typer.Exit(_EXIT_ERROR)


# --------------------------------------------------------------------------- #
# plugins                                                                    #
# --------------------------------------------------------------------------- #

plugins_app = typer.Typer(
    name="plugins",
    help="Inspect and validate plugins.",
    no_args_is_help=True,
)


@plugins_app.command("list")
def plugins_list(
    ctx: typer.Context,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON.")
    ] = False,
) -> None:
    """List every plugin that loaded successfully."""
    with _handle_errors():
        wired = _services(ctx)
        asyncio.run(_plugins_list_async(wired, json_output=json_output))


async def _plugins_list_async(
    wired: WiredServices, *, json_output: bool
) -> None:
    report = await wired.plugin_loader.load_all()
    plugins = wired.plugin_loader.list_plugins()

    if json_output:
        _echo_json(
            {
                "loaded": report.loaded,
                "failed": report.failed,
                "skipped": report.skipped,
                "plugins": [
                    {
                        "name": p.name,
                        "version": p.version,
                        "hooks": list(p.manifest.hooks),
                    }
                    for p in plugins
                ],
            }
        )
        return

    if not plugins:
        _out_console.print("[yellow]No plugins loaded.[/yellow]")
        if report.failed:
            _err_console.print("Failures:")
            for name, err in report.failed.items():
                _err_console.print(f"  [red]✗[/red] {name}: {err}")
        return

    table = Table(title=f"{len(plugins)} plugin(s)")
    table.add_column("Name")
    table.add_column("Version")
    table.add_column("Hooks")
    for plugin in plugins:
        table.add_row(
            plugin.name,
            plugin.version,
            ", ".join(plugin.manifest.hooks),
        )
    _out_console.print(table)
    if report.failed:
        _err_console.print(
            f"[yellow]{len(report.failed)} plugin(s) failed:[/yellow]"
        )
        for name, err in report.failed.items():
            _err_console.print(f"  [red]✗[/red] {name}: {err}")


@plugins_app.command("info")
def plugins_info(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="The plugin name.")],
) -> None:
    """Show the full manifest of a single plugin."""
    with _handle_errors():
        wired = _services(ctx)
        asyncio.run(_plugins_info_async(wired, name))


async def _plugins_info_async(
    wired: WiredServices, name: str
) -> None:
    await wired.plugin_loader.load_all()
    plugin = wired.plugin_loader.get_plugin(name)
    if plugin is None:
        _err_console.print(
            f"[red]Plugin not found:[/red] {name!r}"
        )
        raise typer.Exit(_EXIT_ERROR)

    manifest = plugin.manifest
    _out_console.print(f"[bold]{plugin.name}[/bold]  v{plugin.version}")
    for label, value in (
        ("Author", manifest.author),
        ("License", manifest.license),
        ("Description", manifest.description),
        ("Min host version", manifest.min_nexusdl_version),
        ("Max host version", manifest.max_nexusdl_version or "—"),
        ("Python requires", manifest.python_requires or "—"),
        ("Hooks", ", ".join(manifest.hooks)),
        ("Dependencies", ", ".join(manifest.dependencies) or "—"),
        ("Directory", str(plugin.directory)),
    ):
        _out_console.print(f"  [cyan]{label:<18}[/cyan] {value}")


@plugins_app.command("check")
def plugins_check(
    ctx: typer.Context,
) -> None:
    """Load every plugin strictly and report the first failure."""
    with _handle_errors():
        wired = _services(ctx)
        asyncio.run(_plugins_check_async(wired))


async def _plugins_check_async(wired: WiredServices) -> None:
    report = await wired.plugin_loader.load_all(strict=True)
    _out_console.print(
        f"[green]✓[/green] {len(report.loaded)} plugin(s) loaded cleanly."
    )


# --------------------------------------------------------------------------- #
# admin                                                                      #
# --------------------------------------------------------------------------- #

admin_app = typer.Typer(
    name="admin",
    help="Administrative operations: bootstrap, maintenance.",
    no_args_is_help=True,
)


@admin_app.command("init")
def admin_init(
    ctx: typer.Context,
    path: Annotated[
        Path,
        typer.Option(
            "--path", "-p",
            help="Destination path for the .env file.",
        ),
    ] = Path.cwd() / ".env",
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Overwrite an existing .env file.",
        ),
    ] = False,
) -> None:
    """Create a fresh ``.env`` file with generated secrets."""
    with _handle_errors():
        from nexusdl.core.config import bootstrap_env_file

        if path.exists() and not force:
            _err_console.print(
                f"[yellow].env already exists at[/yellow] {path}. "
                "Pass --force to overwrite."
            )
            raise typer.Exit(_EXIT_ERROR)

        bootstrap_env_file(path, overwrite=force)
        _out_console.print(
            f"[green]✓[/green] Created {path}"
        )
        _out_console.print(
            "Next step: run `nexusdl config validate` to verify."
        )


@admin_app.command("regenerate-secrets")
def admin_regenerate_secrets(
    ctx: typer.Context,
    path: Annotated[
        Path,
        typer.Option("--path", "-p", help="Path to the .env file."),
    ] = Path.cwd() / ".env",
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip the confirmation prompt."),
    ] = False,
) -> None:
    """Regenerate the three secrets in an existing ``.env`` file."""
    with _handle_errors():
        from nexusdl.core.config import bootstrap_env_file

        if not yes:
            _out_console.print(
                "[red]This will invalidate every stored cookie.[/red] "
                f"The .env at {path} will be overwritten with new "
                "secrets."
            )
            confirmed = typer.confirm(
                "Continue?", default=False
            )
            if not confirmed:
                _out_console.print("[yellow]Cancelled.[/yellow]")
                raise typer.Exit(0)

        bootstrap_env_file(path, overwrite=True)
        _out_console.print(
            f"[green]✓[/green] Regenerated {path}"
        )


@admin_app.command("clean")
def admin_clean(
    ctx: typer.Context,
    cache: Annotated[
        bool, typer.Option("--cache/--no-cache", help="Clear the HTTP cache.")
    ] = True,
    logs: Annotated[
        bool, typer.Option("--logs/--no-logs", help="Clear the log files.")
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")
    ] = False,
) -> None:
    """Delete cached or log data to reclaim disk space."""
    with _handle_errors():
        wired = _services(ctx)
        settings = wired.settings

        targets: list[tuple[str, Path]] = []
        if cache:
            targets.append(("cache", settings.paths.cache_dir))
        if logs:
            targets.append(("logs", settings.paths.logs_dir))

        if not targets:
            _out_console.print(
                "[yellow]Nothing selected. Pass --cache and/or --logs.[/yellow]"
            )
            raise typer.Exit(0)

        if not yes:
            _out_console.print("About to delete:")
            for label, p in targets:
                _out_console.print(f"  {label}: {p}")
            confirmed = typer.confirm("Continue?", default=False)
            if not confirmed:
                _out_console.print("[yellow]Cancelled.[/yellow]")
                raise typer.Exit(0)

        total_files = 0
        total_bytes = 0
        for label, root in targets:
            if not root.exists():
                _out_console.print(
                    f"[dim]{label}:[/dim] {root} (absent)"
                )
                continue
            files = 0
            size = 0
            for path in root.rglob("*"):
                if path.is_file():
                    files += 1
                    try:
                        size += path.stat().st_size
                    except OSError:
                        pass
            shutil.rmtree(root, ignore_errors=True)
            total_files += files
            total_bytes += size
            _out_console.print(
                f"[green]✓[/green] {label}: cleared {files} file(s) "
                f"({human_readable_size(size)})"
            )

        _out_console.print(
            f"[bold]Total:[/bold] {total_files} file(s), "
            f"{human_readable_size(total_bytes)} freed."
        )


# --------------------------------------------------------------------------- #
# Serialization helpers                                                      #
# --------------------------------------------------------------------------- #


def _serialize_manga(manga: Manga) -> dict[str, Any]:
    """Serialize a :class:`Manga` for ``--json`` output."""
    return {
        "id": manga.id,
        "site_id": manga.site_id,
        "source_id": manga.source_id,
        "title": manga.title,
        "url": manga.url,
        "language": manga.language.value,
        "status": manga.status.value,
        "content_rating": manga.content_rating.value,
        "is_manga": manga.is_manga,
        "author": manga.author,
        "artist": manga.artist,
        "year": manga.year,
        "description": manga.description,
        "cover_url": manga.cover_url,
        "tags": list(manga.tags),
        "chapter_count": manga.chapter_count,
        "page_count": manga.page_count,
        "created_at": (
            manga.created_at.isoformat() if manga.created_at else None
        ),
        "updated_at": (
            manga.updated_at.isoformat() if manga.updated_at else None
        ),
        "last_read_at": (
            manga.last_read_at.isoformat() if manga.last_read_at else None
        ),
    }


def _serialize_chapter(chapter: Chapter) -> dict[str, Any]:
    """Serialize a :class:`Chapter` for ``--json`` output."""
    return {
        "id": chapter.id,
        "manga_id": chapter.manga_id,
        "source_id": chapter.source_id,
        "title": chapter.title,
        "number": chapter.number,
        "volume": chapter.volume,
        "language": chapter.language.value,
        "page_count": chapter.page_count,
        "url": chapter.url,
    }


#: The three root secrets whose values must never appear in CLI output.
_ROOT_SECRET_FIELDS: tuple[str, ...] = (
    "secret_key",
    "jwt_secret",
    "cookie_encryption_key",
)


def _masked_settings(settings: Any) -> dict[str, Any]:
    """Serialize ``Settings`` for ``--json`` output, with secrets masked.

    Uses :meth:`pydantic.BaseModel.model_dump` to get the full settings
    tree (every section and every field), then redacts the three root
    secrets and the two proxy URL fields. The redaction markers differ
    deliberately:

    * **Root secrets** → ``"***"`` — the length is withheld because a
      JSON file may be committed, and a root secret's length narrows a
      brute-force.
    * **Proxy URLs** → ``"***"`` — same rationale, applied to every URL
      in ``proxy.url`` / ``proxy.urls``. ``None`` stays ``None``.

    The generic dump approach (rather than a hand-enumerated dict of
    sections) means a future config section is automatically included
    without a code change here, and the function cannot drift out of
    sync with the config schema.

    Args:
        settings: A :class:`~nexusdl.core.config.Settings` instance.

    Returns:
        A JSON-serialisable mapping with the same shape as the settings
        tree, minus the secret values.
    """
    data: dict[str, Any] = settings.model_dump(mode="json")

    for field in _ROOT_SECRET_FIELDS:
        if field in data:
            data[field] = "***"

    proxy = data.get("proxy")
    if isinstance(proxy, dict):
        if proxy.get("url"):
            proxy["url"] = "***"
        urls = proxy.get("urls")
        if isinstance(urls, list) and urls:
            proxy["urls"] = ["***"] * len(urls)

    return data


def _print_masked_settings(settings: Any) -> None:
    """Print the effective settings as a Rich tree, with secrets masked.

    Renders one branch per settings section (``app``, ``http``,
    ``downloader``, …), with each field on its own line. The three
    root secrets and the proxy URLs are printed as ``"***"`` so a user
    showing their terminal to a colleague or pasting it into a bug
    report never leaks credentials.

    Args:
        settings: A :class:`~nexusdl.core.config.Settings` instance.
    """
    data = _masked_settings(settings)
    tree = Tree(f"[bold]{APP_NAME} configuration[/bold]")

    # Root scalar fields first (secrets, URLs, etc.) — they are not
    # sections and are useful to see at a glance.
    root_scalars: dict[str, Any] = {
        k: v for k, v in data.items() if not isinstance(v, dict)
    }
    if root_scalars:
        root_branch = tree.add("[cyan]root[/cyan]")
        for key, value in root_scalars.items():
            root_branch.add(f"{key}: [yellow]{value!r}[/yellow]")

    # Then each section as its own branch.
    for section, values in data.items():
        if not isinstance(values, dict):
            continue
        branch = tree.add(f"[cyan]{section}[/cyan]")
        for key, value in values.items():
            branch.add(f"{key}: [yellow]{value!r}[/yellow]")

    _out_console.print(tree)
