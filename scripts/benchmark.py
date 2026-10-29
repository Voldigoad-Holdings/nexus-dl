#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""NexusDL performance benchmarks.

Universal, cross-platform benchmark suite for NexusDL.

Measures:

- **Search latency** per site (min, p50, p95, p99, max, mean)
- **Download throughput** per site (pages/s, MB/s)
- **Packaging time** per format (FOLDER, ZIP, CBZ, CB7, PDF, CBT)
- **Memory usage** (RSS, VMS, GC objects)

Cross-platform notes:

- **Linux/macOS**: uses `resource.getrusage()` for RSS
- **Windows**: uses `psutil` (declared in `[dev]` extra)
- **Fallback**: returns 0.0 with a warning if neither is available

Usage::

    # Full benchmark (network required)
    python scripts/benchmark.py

    # Offline (no network): packaging + memory only
    python scripts/benchmark.py --offline

    # Specific section
    python scripts/benchmark.py --section search --iterations 5
    python scripts/benchmark.py --section packaging --pages 50
    python scripts/benchmark.py --section memory

    # Specific sites
    python scripts/benchmark.py --sites mangadex,mangafire

    # All sites (60+ — slow!)
    python scripts/benchmark.py --sites all

    # Reproducible
    python scripts/benchmark.py --seed 42

    # Dry-run (validate args, no execution)
    python scripts/benchmark.py --dry-run

    # Export JSON
    python scripts/benchmark.py --output results.json

    # Verbose
    python scripts/benchmark.py -v

Exit codes::

    0   Success
    1   One or more benchmarks failed
    2   Invalid arguments
    3   Import error (nexusdl not installed)
    130 Interrupted (Ctrl+C)

Requirements::

    pip install -e ".[dev,cli]"

Make the script executable::

    chmod +x scripts/benchmark.py
"""
from __future__ import annotations

import argparse
import asyncio
import gc
import json
import os
import random
import statistics
import sys
import tempfile
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

# ────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
_SRC_DIR: Final[Path] = _REPO_ROOT / "src"

# Allowed sections
_VALID_SECTIONS: Final[tuple[str, ...]] = (
    "all",
    "search",
    "download",
    "packaging",
    "memory",
)

# Allowed formats
_VALID_FORMATS: Final[tuple[str, ...]] = (
    "FOLDER",
    "ZIP",
    "CBZ",
    "CB7",
    "PDF",
    "CBT",
)

# Default values
_DEFAULT_QUERY: Final[str] = "one piece"
_DEFAULT_ITERATIONS: Final[int] = 3
_DEFAULT_PAGES: Final[int] = 20
_DEFAULT_WARMUP: Final[int] = 1
_DEFAULT_TIMEOUT: Final[float] = 60.0
_DEFAULT_SEED: Final[int] = 42


# ────────────────────────────────────────────────────────────────────
# Import check (clear error if nexusdl not installed)
# ────────────────────────────────────────────────────────────────────

if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

try:
    import nexusdl  # noqa: F401
    from nexusdl.core.config import load_settings
    from nexusdl.core.registry.site_registry import SiteRegistry
    from nexusdl.core.session.http_session import HttpSession
    from nexusdl.parsers.factory import DefaultParserFactory
except ImportError as exc:
    print(f"❌ Cannot import nexusdl: {exc}", file=sys.stderr)
    print(f"   Install with: pip install -e '{_REPO_ROOT}[dev,cli]'", file=sys.stderr)
    sys.exit(3)


if TYPE_CHECKING:
    from nexusdl.core.models.manga import Manga


# ────────────────────────────────────────────────────────────────────
# Data classes
# ────────────────────────────────────────────────────────────────────


@dataclass
class LatencyResult:
    """Search/download latency for one site.

    Attributes:
        site_id: The site tested.
        operation: ``search`` or ``download``.
        iterations: Number of iterations.
        status: ``ok``, ``failed``, ``skipped``.
        latencies_ms: Sorted latencies in milliseconds.
        error: Error message if ``status != "ok"``.
    """

    site_id: str
    operation: str
    iterations: int
    status: str
    latencies_ms: list[float] = field(default_factory=list)
    error: str | None = None

    @property
    def min_ms(self) -> float:
        return min(self.latencies_ms) if self.latencies_ms else 0.0

    @property
    def max_ms(self) -> float:
        return max(self.latencies_ms) if self.latencies_ms else 0.0

    @property
    def mean_ms(self) -> float:
        return statistics.mean(self.latencies_ms) if self.latencies_ms else 0.0

    @property
    def p50_ms(self) -> float:
        return statistics.median(self.latencies_ms) if self.latencies_ms else 0.0

    @property
    def p95_ms(self) -> float:
        return _percentile(self.latencies_ms, 0.95)

    @property
    def p99_ms(self) -> float:
        return _percentile(self.latencies_ms, 0.99)


@dataclass
class PackagingResult:
    """Result of one packaging benchmark."""

    format: str
    page_count: int
    total_bytes: int
    output_bytes: int
    duration_seconds: float
    status: str = "ok"
    error: str | None = None

    @property
    def ratio_percent(self) -> float:
        return (self.output_bytes / self.total_bytes * 100) if self.total_bytes else 0.0

    @property
    def pages_per_second(self) -> float:
        return (self.page_count / self.duration_seconds) if self.duration_seconds > 0 else 0.0


@dataclass
class BenchmarkReport:
    """Aggregated benchmark report."""

    version: str
    timestamp: str
    python_version: str
    platform: str
    section: str
    seed: int
    latency: list[LatencyResult] = field(default_factory=list)
    packaging: list[PackagingResult] = field(default_factory=list)
    memory: dict[str, float] = field(default_factory=dict)
    summary: str = ""
    warnings: list[str] = field(default_factory=list)


# ────────────────────────────────────────────────────────────────────
# Utilities
# ────────────────────────────────────────────────────────────────────


def _percentile(values: list[float], p: float) -> float:
    """Compute the p-th percentile of a list.

    Args:
        values: List of numbers (unsorted).
        p: Percentile in ``[0, 1)``.

    Returns:
        The percentile value (``0.0`` if empty).
    """
    if not values:
        return 0.0
    if not (0.0 <= p < 1.0):
        raise ValueError(f"p must be in [0, 1), got {p}")
    sorted_values = sorted(values)
    k = (len(sorted_values) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)
    if f == c:
        return sorted_values[f]
    return sorted_values[f] * (c - k) + sorted_values[c] * (k - f)


def _format_ms(ms: float) -> str:
    """Format milliseconds for display."""
    if ms < 0.001:
        return f"{ms * 1_000_000:.0f}ns"
    if ms < 1:
        return f"{ms * 1000:.0f}µs"
    if ms < 1000:
        return f"{ms:.1f}ms"
    return f"{ms / 1000:.2f}s"


def _format_bytes(n: int | float) -> str:
    """Format bytes for display."""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def _now_iso() -> str:
    """ISO 8601 UTC timestamp."""
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _safe_asdict(obj: Any) -> Any:
    """Recursively convert dataclasses to JSON-safe dicts.

    Unlike :func:`dataclasses.asdict`, this handles dataclasses nested
    inside lists and dicts, and preserves non-dataclass values as-is.

    Args:
        obj: Any object.

    Returns:
        A JSON-serializable structure.
    """
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _safe_asdict(v) for k, v in asdict(obj).items()}
    if isinstance(obj, (list, tuple)):
        return [_safe_asdict(item) for item in obj]
    if isinstance(obj, dict):
        return {k: _safe_asdict(v) for k, v in obj.items()}
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


# ────────────────────────────────────────────────────────────────────
# Memory detection (cross-platform, no silent failure)
# ────────────────────────────────────────────────────────────────────


def _detect_memory_backend() -> str:
    """Detect the available memory backend.

    Returns:
        ``"resource"``, ``"psutil"``, or ``"none"``.
    """
    if sys.platform != "win32":
        try:
            import resource  # noqa: F401

            return "resource"
        except ImportError:
            pass
    try:
        import psutil  # type: ignore[import-not-found]  # noqa: F401

        return "psutil"
    except ImportError:
        return "none"


_MEMORY_BACKEND: Final[str] = _detect_memory_backend()


def bench_memory() -> dict[str, float]:
    """Measure baseline memory usage.

    Returns:
        Dict with ``rss_mb``, ``vms_mb``, ``objects`` keys.
        Values are ``0.0`` if no memory backend is available.
    """
    gc.collect()

    rss_mb = 0.0
    vms_mb = 0.0

    if _MEMORY_BACKEND == "resource":
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        # On Linux, ru_maxrss is in KB; on macOS, in bytes
        if sys.platform == "darwin":
            rss_mb = usage.ru_maxrss / (1024 * 1024)
        else:
            rss_mb = usage.ru_maxrss / 1024
        # resource doesn't report VMS
        vms_mb = 0.0
    elif _MEMORY_BACKEND == "psutil":
        import psutil

        process = psutil.Process(os.getpid())
        mem = process.memory_info()
        rss_mb = mem.rss / (1024 * 1024)
        vms_mb = mem.vms / (1024 * 1024)

    return {
        "rss_mb": round(rss_mb, 2),
        "vms_mb": round(vms_mb, 2),
        "objects": float(len(gc.get_objects())),
    }


# ────────────────────────────────────────────────────────────────────
# Benchmarks: search
# ────────────────────────────────────────────────────────────────────


async def _bench_one_site_search(
    site_id: str,
    registry: SiteRegistry,
    settings: Any,
    query: str,
    iterations: int,
    warmup: int,
    timeout: float,
) -> LatencyResult:
    """Benchmark a single site's search.

    Args:
        site_id: Site ID to test.
        registry: Loaded ``SiteRegistry``.
        settings: Application settings.
        query: Search query.
        iterations: Number of measured iterations.
        warmup: Number of warm-up iterations (not measured).
        timeout: Per-iteration timeout in seconds.

    Returns:
        A ``LatencyResult``.
    """
    try:
        site = registry.get_site(site_id)
    except KeyError:
        return LatencyResult(
            site_id=site_id,
            operation="search",
            iterations=iterations,
            status="skipped",
            error="Site not found in registry",
        )

    try:
        async with HttpSession(site, settings) as session:
            try:
                parser = registry.get_parser(site_id, session)
            except Exception as exc:  # noqa: BLE001
                return LatencyResult(
                    site_id=site_id,
                    operation="search",
                    iterations=iterations,
                    status="failed",
                    error=f"Parser not available: {exc}",
                )

            # Warm-up (not measured, failures ignored)
            for _ in range(warmup):
                try:
                    await asyncio.wait_for(parser.search(query), timeout=timeout)
                except Exception:  # noqa: BLE001
                    pass

            # Measured iterations
            latencies: list[float] = []
            for _ in range(iterations):
                t0 = time.perf_counter()
                try:
                    await asyncio.wait_for(parser.search(query), timeout=timeout)
                except asyncio.TimeoutError:
                    return LatencyResult(
                        site_id=site_id,
                        operation="search",
                        iterations=iterations,
                        status="failed",
                        error=f"Timeout after {timeout}s",
                    )
                except Exception as exc:  # noqa: BLE001
                    return LatencyResult(
                        site_id=site_id,
                        operation="search",
                        iterations=iterations,
                        status="failed",
                        error=str(exc),
                    )
                latencies.append((time.perf_counter() - t0) * 1000)

            return LatencyResult(
                site_id=site_id,
                operation="search",
                iterations=iterations,
                status="ok",
                latencies_ms=sorted(latencies),
            )

    except Exception as exc:  # noqa: BLE001
        return LatencyResult(
            site_id=site_id,
            operation="search",
            iterations=iterations,
            status="failed",
            error=f"Session error: {exc}",
        )


async def bench_search(
    sites: list[str],
    query: str,
    iterations: int,
    warmup: int,
    timeout: float,
    verbose: bool = False,
) -> list[LatencyResult]:
    """Measure search latency per site.

    Args:
        sites: Site IDs to test. Empty list = all.
        query: Search query.
        iterations: Measured iterations per site.
        warmup: Warm-up iterations per site.
        timeout: Per-iteration timeout.
        verbose: Print progress.

    Returns:
        One ``LatencyResult`` per site.
    """
    settings = load_settings()
    registry = SiteRegistry(settings, parser_factory=DefaultParserFactory())

    target_sites = sites or [s.id for s in registry.list_sites()]
    results: list[LatencyResult] = []

    for i, site_id in enumerate(target_sites, 1):
        if verbose:
            print(f"  [{i}/{len(target_sites)}] {site_id}...", end="", flush=True)

        result = await _bench_one_site_search(
            site_id=site_id,
            registry=registry,
            settings=settings,
            query=query,
            iterations=iterations,
            warmup=warmup,
            timeout=timeout,
        )
        results.append(result)

        if verbose:
            if result.status == "ok":
                print(f" ✓ {_format_ms(result.p50_ms)} (p50)")
            else:
                print(f" ✗ {result.error}")

    return results


# ────────────────────────────────────────────────────────────────────
# Benchmarks: packaging
# ────────────────────────────────────────────────────────────────────


def _generate_pages(
    directory: Path,
    count: int,
    seed: int,
    grayscale: bool = False,
) -> list[Path]:
    """Generate fake manga pages (JPEG).

    Args:
        directory: Where to write the pages.
        count: Number of pages.
        seed: Random seed (for reproducibility).
        grayscale: If True, generate grayscale pages (smaller).

    Returns:
        List of generated file paths.
    """
    from PIL import Image

    rng = random.Random(seed)
    mode = "L" if grayscale else "RGB"
    pages: list[Path] = []

    for i in range(count):
        page_path = directory / f"{i + 1:03d}.jpg"
        width, height = 800, 1200

        # Generate a realistic-looking page (noise + border)
        if grayscale:
            img = Image.new("L", (width, height))
            pixels = [
                rng.randint(200, 255) if (i % 10 == 0 or j % 50 == 0) else rng.randint(0, 50)
                for i, j in [(y, x) for y in range(height) for x in range(width)]
            ][: width * height]
            img.putdata(pixels)
        else:
            img = Image.new("RGB", (width, height))
            pixels = [
                (
                    rng.randint(200, 255),
                    rng.randint(200, 255),
                    rng.randint(200, 255),
                )
                if (i % 10 == 0 or j % 50 == 0)
                else (rng.randint(0, 50), rng.randint(0, 50), rng.randint(0, 50))
                for i, j in [(y, x) for y in range(height) for x in range(width)]
            ][: width * height]
            img.putdata(pixels)

        img.save(page_path, "JPEG", quality=85)
        pages.append(page_path)

    return pages


async def bench_packaging(
    page_count: int,
    formats: list[str],
    seed: int,
    verbose: bool = False,
) -> list[PackagingResult]:
    """Measure packaging time per format.

    Args:
        page_count: Number of pages per archive.
        formats: Formats to test.
        seed: Random seed for image generation.
        verbose: Print progress.

    Returns:
        One ``PackagingResult`` per format.
    """
    try:
        from nexusdl.core.models.enums import PackagingFormat
        from nexusdl.core.packaging.factory import PackagerFactory
    except ImportError as exc:
        return [
            PackagingResult(
                format="ALL",
                page_count=0,
                total_bytes=0,
                output_bytes=0,
                duration_seconds=0.0,
                status="failed",
                error=f"Import error: {exc}",
            )
        ]

    factory = PackagerFactory()
    results: list[PackagingResult] = []

    with tempfile.TemporaryDirectory(prefix="nexusdl-bench-") as tmp:
        tmp_path = Path(tmp)
        pages_dir = tmp_path / "pages"
        pages_dir.mkdir()

        if verbose:
            print(f"  Generating {page_count} fake pages (seed={seed})...", end="", flush=True)
        pages = _generate_pages(pages_dir, page_count, seed)
        total_bytes = sum(p.stat().st_size for p in pages)
        if verbose:
            print(f" done ({_format_bytes(total_bytes)})")

        for fmt_name in formats:
            try:
                fmt = PackagingFormat(fmt_name)
            except ValueError:
                results.append(
                    PackagingResult(
                        format=fmt_name,
                        page_count=page_count,
                        total_bytes=total_bytes,
                        output_bytes=0,
                        duration_seconds=0.0,
                        status="failed",
                        error=f"Unknown format: {fmt_name}",
                    )
                )
                continue

            if verbose:
                print(f"  [{fmt_name}]", end="", flush=True)

            output_path: Path | None = None
            try:
                packager = factory.create(fmt)
                output_path = tmp_path / f"output.{fmt_name.lower()}"

                t0 = time.perf_counter()
                result_path = await packager.package(pages, output_path)
                duration = time.perf_counter() - t0

                output_bytes = result_path.stat().st_size if result_path.is_file() else 0

                results.append(
                    PackagingResult(
                        format=fmt_name,
                        page_count=page_count,
                        total_bytes=total_bytes,
                        output_bytes=output_bytes,
                        duration_seconds=duration,
                    )
                )

                if verbose:
                    ratio = (output_bytes / total_bytes * 100) if total_bytes else 0
                    print(f" ✓ {duration:.2f}s, {ratio:.1f}% ratio")

            except Exception as exc:  # noqa: BLE001
                results.append(
                    PackagingResult(
                        format=fmt_name,
                        page_count=page_count,
                        total_bytes=total_bytes,
                        output_bytes=0,
                        duration_seconds=0.0,
                        status="failed",
                        error=str(exc),
                    )
                )
                if verbose:
                    print(f" ✗ {exc}")

            finally:
                # Cleanup, even on error
                if output_path is not None:
                    try:
                        if output_path.is_dir():
                            import shutil

                            shutil.rmtree(output_path)
                        elif output_path.exists():
                            output_path.unlink()
                    except OSError:
                        pass

    return results


# ────────────────────────────────────────────────────────────────────
# Report
# ────────────────────────────────────────────────────────────────────


def build_report(
    section: str,
    seed: int,
    latency: list[LatencyResult],
    packaging: list[PackagingResult],
    memory: dict[str, float],
) -> BenchmarkReport:
    """Build the aggregated report.

    Args:
        section: Requested section.
        seed: Seed used for the run.
        latency: Latency results.
        packaging: Packaging results.
        memory: Memory usage.

    Returns:
        The populated report.
    """
    try:
        from nexusdl.version import __version__  # type: ignore[import-not-found]
    except ImportError:
        __version__ = "unknown"

    warnings: list[str] = []
    if _MEMORY_BACKEND == "none":
        warnings.append(
            "No memory backend available (install psutil on Windows: pip install psutil)"
        )

    return BenchmarkReport(
        version=__version__,
        timestamp=_now_iso(),
        python_version=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        platform=sys.platform,
        section=section,
        seed=seed,
        latency=latency,
        packaging=packaging,
        memory=memory,
        warnings=warnings,
    )


def _compute_summary(report: BenchmarkReport) -> str:
    """Compute a one-line summary.

    Args:
        report: The report.

    Returns:
        A one-line summary.
    """
    parts: list[str] = []

    if report.latency:
        ok = [r for r in report.latency if r.status == "ok"]
        if ok:
            avg_p50 = statistics.mean(r.p50_ms for r in ok)
            parts.append(f"Search p50: {_format_ms(avg_p50)} (avg over {len(ok)} sites)")

    if report.packaging:
        ok = [r for r in report.packaging if r.status == "ok"]
        if ok:
            fastest = min(ok, key=lambda r: r.duration_seconds)
            smallest = min(ok, key=lambda r: r.output_bytes)
            parts.append(f"Fastest: {fastest.format} ({fastest.duration_seconds:.2f}s)")
            parts.append(f"Smallest: {smallest.format} ({_format_bytes(smallest.output_bytes)})")

    if report.memory:
        parts.append(f"RSS: {report.memory['rss_mb']:.1f} MB")

    return " · ".join(parts) if parts else "No successful benchmarks"


def print_report(report: BenchmarkReport) -> None:
    """Pretty-print the report to stdout.

    Args:
        report: The report to display.
    """
    print()
    print("═" * 78)
    print(f"  NexusDL Benchmark Report — v{report.version}")
    print("═" * 78)
    print(f"  Timestamp: {report.timestamp}")
    print(f"  Python:    {report.python_version}")
    print(f"  Platform:  {report.platform}")
    print(f"  Section:   {report.section}")
    print(f"  Seed:      {report.seed}")
    print(f"  Memory:    {_MEMORY_BACKEND}")
    print()

    if report.warnings:
        for w in report.warnings:
            print(f"  ⚠️  {w}")
        print()

    if report.latency:
        print("┌─ Latency ────────────────────────────────────────────────────────────┐")
        print(
            f"│ {'Site':<15} {'Op':<8} {'Status':<8} {'min':>8} {'p50':>8} "
            f"{'p95':>8} {'p99':>8} {'mean':>8} │"
        )
        print(
            "├─────────────────┼──────────┼──────────┼──────────┼──────────┼"
            "──────────┼──────────┼──────────┤"
        )
        for r in report.latency:
            if r.status == "ok":
                print(
                    f"│ {r.site_id:<15} {r.operation:<8} {r.status:<8} "
                    f"{_format_ms(r.min_ms):>8} "
                    f"{_format_ms(r.p50_ms):>8} "
                    f"{_format_ms(r.p95_ms):>8} "
                    f"{_format_ms(r.p99_ms):>8} "
                    f"{_format_ms(r.mean_ms):>8} │"
                )
            else:
                print(
                    f"│ {r.site_id:<15} {r.operation:<8} {r.status:<8} "
                    f"{'—':>8} {'—':>8} {'—':>8} {'—':>8} {'—':>8} │"
                )
                if r.error:
                    print(f"│   └─ {r.error[:68]:<68} │")
        print("└──────────────────────────────────────────────────────────────────────┘")
        print()

    if report.packaging:
        print("┌─ Packaging ──────────────────────────────────────────────────────────┐")
        print(
            f"│ {'Format':<8} {'Pages':>6} {'Input':>10} {'Output':>10} "
            f"{'Ratio':>8} {'Time':>9} {'Pages/s':>9} │"
        )
        print(
            "├──────────┼────────┼────────────┼────────────┼──────────┼"
            "───────────┼───────────┤"
        )
        for r in report.packaging:
            if r.status == "ok":
                print(
                    f"│ {r.format:<8} {r.page_count:>6} "
                    f"{_format_bytes(r.total_bytes):>10} "
                    f"{_format_bytes(r.output_bytes):>10} "
                    f"{r.ratio_percent:>7.1f}% "
                    f"{r.duration_seconds:>8.2f}s "
                    f"{r.pages_per_second:>9.1f} │"
                )
            else:
                print(f"│ {r.format:<8} {r.status:<68} │")
                if r.error:
                    print(f"│   └─ {r.error[:68]:<68} │")
        print("└──────────────────────────────────────────────────────────────────────┘")
        print()

    if report.memory:
        print("┌─ Memory ─────────────────────────────────────────────────────────────┐")
        for key, value in report.memory.items():
            print(f"│ {key:<30} {value:>35.2f} │")
        print("└──────────────────────────────────────────────────────────────────────┘")
        print()

    print("═" * 78)
    if report.summary:
        print(f"  {report.summary}")
    print("═" * 78)


# ────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="benchmark",
        description="NexusDL performance benchmarks (cross-platform)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                         # all sections, all sites
  %(prog)s --offline                               # no network
  %(prog)s --section search --iterations 5
  %(prog)s --section packaging --pages 50
  %(prog)s --sites mangadex,mangafire
  %(prog)s --sites all --verbose
  %(prog)s --seed 42
  %(prog)s --output results.json
  %(prog)s --dry-run                               # validate args only

Exit codes: 0 = success, 1 = failures, 2 = bad args, 3 = import error
        """,
    )

    # Section
    parser.add_argument(
        "--section",
        choices=_VALID_SECTIONS,
        default="all",
        help=f"Benchmark section (default: all). Choices: {', '.join(_VALID_SECTIONS)}",
    )

    # Search options
    parser.add_argument(
        "--sites",
        default="",
        help="Comma-separated site IDs, or 'all' (default: all)",
    )
    parser.add_argument(
        "--query",
        default=_DEFAULT_QUERY,
        help=f"Search query (default: {_DEFAULT_QUERY!r})",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=_DEFAULT_ITERATIONS,
        help=f"Measured iterations per site (default: {_DEFAULT_ITERATIONS})",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=_DEFAULT_WARMUP,
        help=f"Warm-up iterations per site (default: {_DEFAULT_WARMUP})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=_DEFAULT_TIMEOUT,
        help=f"Per-iteration timeout in seconds (default: {_DEFAULT_TIMEOUT})",
    )

    # Packaging options
    parser.add_argument(
        "--pages",
        type=int,
        default=_DEFAULT_PAGES,
        help=f"Pages per packaging benchmark (default: {_DEFAULT_PAGES})",
    )
    parser.add_argument(
        "--formats",
        default=",".join(_VALID_FORMATS),
        help=f"Comma-separated formats (default: all). Choices: {', '.join(_VALID_FORMATS)}",
    )

    # Common
    parser.add_argument(
        "--seed",
        type=int,
        default=_DEFAULT_SEED,
        help=f"Random seed for reproducibility (default: {_DEFAULT_SEED})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write JSON results to this file",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Skip network benchmarks (search)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate arguments and exit without running benchmarks",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Verbose output (per-site progress)",
    )

    return parser


def _validate_args(args: argparse.Namespace) -> list[str]:
    """Validate parsed arguments.

    Args:
        args: Parsed arguments.

    Returns:
        List of error messages (empty if OK).
    """
    errors: list[str] = []

    if args.iterations < 1:
        errors.append("--iterations must be >= 1")

    if args.warmup < 0:
        errors.append("--warmup must be >= 0")

    if args.timeout <= 0:
        errors.append("--timeout must be > 0")

    if args.pages < 1:
        errors.append("--pages must be >= 1")

    # Validate formats
    if args.formats:
        formats = [f.strip().upper() for f in args.formats.split(",") if f.strip()]
        invalid = [f for f in formats if f not in _VALID_FORMATS]
        if invalid:
            errors.append(f"Invalid formats: {invalid}. Valid: {list(_VALID_FORMATS)}")

    return errors


async def async_main(args: argparse.Namespace) -> int:
    """Async main entry point.

    Args:
        args: Parsed arguments.

    Returns:
        Exit code.
    """
    print(f"→ NexusDL benchmark (section={args.section}, seed={args.seed})")

    latency_results: list[LatencyResult] = []
    packaging_results: list[PackagingResult] = []
    memory: dict[str, float] = {}

    # Parse sites
    sites: list[str] = []
    if args.sites:
        sites = [s.strip() for s in args.sites.split(",") if s.strip()]
        if sites == ["all"]:
            sites = []

    # ── Search ──
    if args.section in ("all", "search") and not args.offline:
        n_sites = len(sites) if sites else "all"
        print(f"  Search: {n_sites} site(s) × {args.iterations} iter (+ {args.warmup} warmup)")
        latency_results = await bench_search(
            sites=sites,
            query=args.query,
            iterations=args.iterations,
            warmup=args.warmup,
            timeout=args.timeout,
            verbose=args.verbose,
        )
        ok = sum(1 for r in latency_results if r.status == "ok")
        print(f"  Search: {ok}/{len(latency_results)} sites OK")
    elif args.offline:
        print("  Search: skipped (--offline)")

    # ── Packaging ──
    if args.section in ("all", "packaging"):
        formats = [f.strip().upper() for f in args.formats.split(",") if f.strip()]
        print(f"  Packaging: {args.pages} pages × {len(formats)} format(s)")
        packaging_results = await bench_packaging(
            page_count=args.pages,
            formats=formats,
            seed=args.seed,
            verbose=args.verbose,
        )
        ok = sum(1 for r in packaging_results if r.status == "ok")
        print(f"  Packaging: {ok}/{len(packaging_results)} formats OK")

    # ── Memory ──
    if args.section in ("all", "memory"):
        print(f"  Memory: measuring (backend={_MEMORY_BACKEND})")
        memory = bench_memory()

    # ── Build report ──
    report = build_report(
        section=args.section,
        seed=args.seed,
        latency=latency_results,
        packaging=packaging_results,
        memory=memory,
    )
    report.summary = _compute_summary(report)

    # ── Print ──
    print_report(report)

    # ── Export ──
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        data = _safe_asdict(report)
        args.output.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"\n✓ JSON written to {args.output}")

    # ── Exit code ──
    failed = sum(1 for r in latency_results if r.status == "failed")
    failed += sum(1 for r in packaging_results if r.status == "failed")
    return 1 if failed > 0 else 0


def main() -> int:
    """CLI entry point.

    Returns:
        Exit code.
    """
    parser = build_parser()
    args = parser.parse_args()

    # Validate
    errors = _validate_args(args)
    if errors:
        for err in errors:
            print(f"❌ {err}", file=sys.stderr)
        return 2

    # Dry run
    if args.dry_run:
        print("✓ Arguments valid. Dry run complete.")
        print(f"  Section:    {args.section}")
        print(f"  Sites:      {args.sites or 'all'}")
        print(f"  Iterations: {args.iterations}")
        print(f"  Warmup:     {args.warmup}")
        print(f"  Timeout:    {args.timeout}s")
        print(f"  Pages:      {args.pages}")
        print(f"  Formats:    {args.formats}")
        print(f"  Seed:       {args.seed}")
        print(f"  Output:     {args.output or '(none)'}")
        print(f"  Offline:    {args.offline}")
        return 0

    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        print("\n⚠️  Interrupted by user (Ctrl+C)", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"❌ Fatal error: {exc}", file=sys.stderr)
        if args.verbose:
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
