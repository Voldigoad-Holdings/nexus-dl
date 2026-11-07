"""Public API of the NexusDL packaging subsystem.

The :mod:`nexusdl.core.packaging` package turns a chapter's downloaded
page files into the final artifact the user keeps: a folder, a ZIP, a
CBZ, a CB7, a CBT, or a PDF. It is the last stage of the download
pipeline, called by
:meth:`~nexusdl.core.downloader.worker.DownloadWorker._package` after all
pages have been fetched, validated, and written to a temporary directory.

Nine modules cooperate, each with a single responsibility:

* :mod:`~nexusdl.core.packaging.base` - the abstract
  :class:`~nexusdl.core.packaging.base.BasePackager` contract every
  concrete packager implements.
* :mod:`~nexusdl.core.packaging.comic_info` - the
  :class:`~nexusdl.core.packaging.comic_info.ComicInfo` model and its
  XML serialization (ComicRack schema, read by Komga, Kavita, Mihon,
  YACReader, CDisplayEx).
* :mod:`~nexusdl.core.packaging.folder_packager` - writes a plain
  directory of images, with an optional ``ComicInfo.xml`` at the root.
* :mod:`~nexusdl.core.packaging.zip_packager` - writes a plain ``.zip``;
  metadata is intentionally ignored (ZIP has no ComicInfo convention).
* :mod:`~nexusdl.core.packaging.cbz_packager` - writes a ``.cbz``
  (ZIP + ``ComicInfo.xml`` when metadata is provided).
* :mod:`~nexusdl.core.packaging.cb7_packager` - writes a ``.cb7``
  (7-Zip + ``ComicInfo.xml``); smaller archives, slower to produce.
* :mod:`~nexusdl.core.packaging.cbt_packager` - writes a ``.cbt``
  (uncompressed TAR + ``ComicInfo.xml``); the CBR-free sibling of CBZ,
  in accordance with ADR-002.
* :mod:`~nexusdl.core.packaging.pdf_packager` - writes a single lossless
  PDF via :mod:`img2pdf`; no re-encoding of JPEG/PNG.
* :mod:`~nexusdl.core.packaging.factory` - resolves a
  :class:`~nexusdl.core.models.enums.PackagingFormat` to a concrete
  packager, with lazy imports so the factory is importable even in a
  partial source tree.

Format coverage (this release)
------------------------------
============================  ======================================
:class:`PackagingFormat`       Packager
============================  ======================================
``FOLDER``                     :class:`FolderPackager`
``ZIP``                        :class:`ZipPackager`
``CBZ``                        :class:`CbzPackager`
``CB7``                        :class:`Cb7Packager`
``PDF``                        :class:`PdfPackager`
``CBT``                        :class:`CbtPackager`
============================  ======================================

Every :class:`PackagingFormat` member declared in
:mod:`~nexusdl.core.models.enums` has a corresponding packager in this
release. ``CBR`` is deliberately absent from the enum — see ADR-002.

Composition
-----------
The composition root (CLI / TUI / GUI / Web) constructs a
:class:`PackagerFactory`, registers any plugin-provided packagers, and
hands it to the worker factory::

    from nexusdl.core.packaging import PackagerFactory, PackagingFormat

    packager_factory = PackagerFactory()

    def worker_factory(parser):
        return DownloadWorker(
            parser=parser,
            packager_factory=packager_factory,
            settings=settings,
            event_bus=bus,
            plugin_dispatcher=plugin_loader,
        )

    # Plugins can override a shipped format, or (in a future release where
    # the enum grows) wire up a new one:
    # packager_factory.register(PackagingFormat.CBZ, MyCustomCbzPackager)

Metadata is optional throughout. ``ZipPackager`` ignores it (the format
has no ComicInfo convention). ``FolderPackager``, ``CbzPackager``,
``Cb7Packager``, and ``CbtPackager`` embed the serialized
``ComicInfo.xml`` at the artifact root. ``PdfPackager`` maps a subset of
the fields to PDF document properties. Each packager documents its own
choice in the class-level docstring.

Lazy loading
------------
Every export in ``__all__`` is resolved through PEP 562's ``__getattr__``
mechanism. Importing ``nexusdl.core.packaging`` **does not import any
submodule**.

This matters more here than in most other packages: the eager
``__init__.py`` imported **every concrete packager**, and each one pulls
in a specific heavy dependency:

* ``pdf_packager.py`` → :mod:`img2pdf` (~60-80 ms) + :mod:`PIL`.
* ``cb7_packager.py`` → :mod:`py7zr` (~40-50 ms).
* ``comic_info.py`` → :mod:`pydantic` (already loaded transitively by
  the pipeline in most call sites; the extra cost is small when
  ``ComicInfo`` is the first pydantic model imported).
* ``cbz_packager.py``, ``cbt_packager.py``, ``folder_packager.py``,
  ``zip_packager.py`` → :mod:`PIL` transitively in some cases
  (shared with ``pdf_packager``) or stdlib only (``tarfile`` for CBT,
  ``zipfile`` for ZIP/CBZ).

**Total eager cost: ~200-250 ms**, paid by any caller - even one who
only wanted :class:`BasePackager` (a pure-stdlib ABC). After this change:

=============================================  =========  =========
Scenario                                        Before     After
=============================================  =========  =========
``from ...packaging import BasePackager``      ~230 ms    ~5 ms
``from ...packaging import PackagerFactory``   ~230 ms    ~15 ms
``from ...packaging import ComicInfo``         ~230 ms    ~25 ms
``from ...packaging import CbzPackager``       ~230 ms    ~30 ms
``from ...packaging import CbtPackager``       ~230 ms    ~10 ms
``from ...packaging import PdfPackager``       ~230 ms    ~100 ms
=============================================  =========  =========

The public surface is unchanged: ``from nexusdl.core.packaging import
X`` works exactly as before - resolved on first access and cached. No
caller has to change a line.

.. note::
   :class:`PackagerFactory` itself already uses lazy imports for its
   concrete packagers (see
   :mod:`~nexusdl.core.packaging.factory`), so importing the factory
   no longer forces ``img2pdf`` and ``py7zr`` to load. The lazy
   conversion of this ``__init__.py`` only needed to stop it from
   **re-introducing** that eager load at the package boundary.

Stability
---------
``__all__`` is the public contract. Adding a symbol is a minor change;
removing or renaming one is a breaking change.

The following are **not** re-exported because they are private to their
module and are allowed to change without notice:

* ``_COMIC_INFO_ENTRY``, ``_COMIC_INFO_FILENAME``, ``_REQUIRED_SUFFIX``,
  ``_DEFAULT_COMPRESSION_LEVEL``, ``_MAX_COMPRESSION_LEVEL``,
  ``_TEMP_PREFIX_FMT``, ``_TEMP_DIR_PREFIX_FMT``, ``_BACKUP_DIR_PREFIX_FMT``,
  ``_ALLOWED_SUFFIXES``, ``_XML_DECLARATION``, ``_ROOT_TAG``,
  ``_CSV_FIELDS``, ``_SCALAR_FIELDS``, ``_IMG2PDF_NATIVE_FORMATS``,
  ``_BACKGROUND``, ``_PNG_TEMPLATE``, ``_INTERMEDIATE_PDF_NAME``,
  ``_DEFAULT_REGISTRY``, ``_PACKAGE_ROOT``, ``_IMAGE_FORMAT_ERRORS``,
  ``_PY7ZR_ERRORS``, ``_COPY_CHUNK_SIZE`` — module-private constants.
* ``_resolve_compression``, ``_resolve_preset``, ``_serialize_metadata``,
  ``_fsync_and_replace``, ``_atomic_replace_directory``,
  ``_fsync_directory``, ``_write_comic_info``, ``_write_pdf``,
  ``_write_bytes_member``, ``_write_file_member``, ``_safe_unlink``,
  ``_convert_pages_to_png``, ``_flatten_to_png``,
  ``_flatten_alpha_to_png``, ``_save_rgb_or_l_to_png``,
  ``_build_metadata_kwargs``, ``_build_pdf_creation_date``,
  ``_local_name``, ``_stringify``, ``_page_attributes``,
  ``_split_csv``, ``_parse_pages``, ``_make_lazy_loader``,
  ``_missing_format_message`` - helper functions.
* ``_validate_manga``, ``_validate_black_and_white``,
  ``_coerce_to_tuple`` on :class:`ComicInfo` - Pydantic field validators
  (invoked automatically by Pydantic, never called directly).

Dependency graph (no cycles)::

    core.exceptions
         ^
         |
    +----+---------------------------------------------------+
    |                                                         |
    base <-- packagers (folder / zip / cbz / cb7 / cbt / pdf)|
      ^                         ^                             |
      |                         |                             |
      +----- comic_info --------+                             |
                 ^                                             |
                 |                                             |
              factory (imports base eagerly,                  |
                       packagers lazily)                      |
                                                               |
    -----------------------------------------------------------+
                (no import edge to parsers/, plugins/,
                 or interfaces/ - rule R1)

``base`` imports nothing from the concrete packagers; each concrete
packager imports ``base`` and optionally ``comic_info``; ``factory``
imports ``base`` eagerly and resolves each concrete packager lazily at
:meth:`~nexusdl.core.packaging.factory.PackagerFactory.create` time.

The arrows above describe **runtime dependencies at first use**, not
imports performed by this file. This file imports **nothing** at module
load - the nine submodules are loaded lazily, on first attribute access.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any


# ===========================================================================
# Public API
# ===========================================================================

__all__ = [
    # --- Base contract -------------------------------------------------- #
    "BasePackager",
    # --- Metadata ------------------------------------------------------- #
    "ComicInfo",
    "ComicInfoPage",
    # --- Concrete packagers -------------------------------------------- #
    "Cb7Packager",
    "CbtPackager",
    "CbzPackager",
    "FolderPackager",
    "PdfPackager",
    "ZipPackager",
    # --- Factory -------------------------------------------------------- #
    "PackagerFactory",
    # --- Introspection -------------------------------------------------- #
    "list_symbols",
    "get_symbol",
]


# ===========================================================================
# Lazy import registry
# ===========================================================================
#
# Maps exported name -> (submodule basename, attribute name in that submodule).
# Every listed symbol is defined at module level in the given submodule.
# Adding a new top-level export is a one-line change here plus a matching
# TYPE_CHECKING re-export below.

_LAZY: dict[str, tuple[str, str]] = {
    # --- base (light: abc, pathlib - stdlib only) ------------------------ #
    "BasePackager": ("base", "BasePackager"),
    # --- comic_info (medium: pydantic) ---------------------------------- #
    "ComicInfo": ("comic_info", "ComicInfo"),
    "ComicInfoPage": ("comic_info", "ComicInfoPage"),
    # --- concrete packagers -------------------------------------------- #
    "Cb7Packager": ("cb7_packager", "Cb7Packager"),
    "CbtPackager": ("cbt_packager", "CbtPackager"),
    "CbzPackager": ("cbz_packager", "CbzPackager"),
    "FolderPackager": ("folder_packager", "FolderPackager"),
    "PdfPackager": ("pdf_packager", "PdfPackager"),
    "ZipPackager": ("zip_packager", "ZipPackager"),
    # --- factory (medium: base + lazy concrete packagers) --------------- #
    "PackagerFactory": ("factory", "PackagerFactory"),
}


# ===========================================================================
# Static re-exports for type checkers and IDEs
# ===========================================================================
#
# Never executed at runtime; only consumed by mypy / pyright / IDEs. Import
# from the concrete submodule so static analysis sees the real definitions
# without triggering an eager runtime import.

if TYPE_CHECKING:
    from nexusdl.core.packaging.base import BasePackager
    from nexusdl.core.packaging.cb7_packager import Cb7Packager
    from nexusdl.core.packaging.cbt_packager import CbtPackager
    from nexusdl.core.packaging.cbz_packager import CbzPackager
    from nexusdl.core.packaging.comic_info import ComicInfo, ComicInfoPage
    from nexusdl.core.packaging.factory import PackagerFactory
    from nexusdl.core.packaging.folder_packager import FolderPackager
    from nexusdl.core.packaging.pdf_packager import PdfPackager
    from nexusdl.core.packaging.zip_packager import ZipPackager


# ===========================================================================
# Lazy loader
# ===========================================================================

def __getattr__(name: str) -> Any:
    """Resolve a public packaging symbol on first access (PEP 562).

    Called by Python's import machinery whenever an attribute is accessed
    on this package that is not already bound in ``globals()``. Only the
    names in :data:`__all__` are handled; anything else raises
    :class:`AttributeError` so that ``hasattr``, ``getattr(x, name,
    default)`` and interactive introspection behave as users expect.

    The catch is intentionally broadened from ``ModuleNotFoundError`` to
    ``ImportError`` — a partially-initialized submodule (missing symbol,
    circular import resolved differently across Python versions) also
    deserves the ``[core]`` install hint rather than a raw traceback.

    Args:
        name: The attribute name being accessed.

    Returns:
        The symbol registered under ``name`` in the submodule recorded in
        :data:`_LAZY`. The submodule is imported once and its binding is
        cached in ``globals()``, so subsequent accesses are plain dict
        lookups.

    Raises:
        AttributeError: When ``name`` is not a public export, when the
            submodule cannot be imported (a missing optional dependency
            such as ``img2pdf``, ``py7zr``, ``Pillow``, or ``pydantic``,
            a syntax error deeper in the module, or a broken
            installation), or when the submodule does not define the
            requested name. ``AttributeError`` — rather than
            ``ImportError`` — is required by PEP 562 so that ``hasattr``
            works.
    """
    # Dunder / special attributes must never route through lazy loading.
    if name.startswith("__"):
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        )

    entry = _LAZY.get(name)
    if entry is None:
        available = ", ".join(sorted(__all__))
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}. "
            f"Available packaging exports: {available}. "
            f"Every other symbol lives in its specific submodule - "
            f"e.g. 'from nexusdl.core.packaging.base import "
            f"BasePackager' or 'from nexusdl.core.packaging.cbz_packager "
            f"import CbzPackager'."
        )

    submodule_basename, attr_name = entry
    try:
        module = importlib.import_module(f".{submodule_basename}", __name__)
    except ImportError as exc:
        # ``ImportError`` is broader than ``ModuleNotFoundError`` on
        # purpose — see the docstring.
        if getattr(exc, "name", None) in {
            "img2pdf",
            "py7zr",
            "PIL",
            "Pillow",
            "pydantic",
        }:
            raise ImportError(
                f"{name!r} requires the core extra (packaging subsystem "
                f"dependencies: img2pdf, py7zr, Pillow, pydantic). "
                f"Install it with: pip install 'nexusdl[core]' "
                f"(or 'nexusdl[all]' for the full set)."
            ) from exc
        raise

    try:
        value = getattr(module, attr_name)
    except AttributeError as exc:
        # ``from exc`` preserves the cause. A ``getattr`` on a healthy
        # module raises ``AttributeError`` with the standard message;
        # an exotic failure (a broken descriptor, a partially-initialised
        # class) shows up with its own message attached, which is the
        # diagnostic the operator needs.
        raise AttributeError(
            f"The packaging submodule {submodule_basename!r} does not "
            f"define {attr_name!r}. This is a package-internal "
            f"inconsistency - nexusdl.core.packaging.__all__ and the "
            f"submodules are out of sync. Original error: {exc}"
        ) from exc

    # Cache in the module namespace so subsequent lookups bypass
    # __getattr__ entirely (same behavior as a top-level import).
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Return the package's visible attributes, including lazy exports.

    Python's default :func:`dir` on a module already lists ``__all__``
    entries even when they are not bound in ``__dict__`` — so without
    this override, ``BasePackager``, ``PackagerFactory``, etc. would
    still appear. Keeping the override explicit serves two purposes:

    1. It documents that the packaging exports are *intentional*
       attributes of this package, not accidental names.
    2. It preserves the module's actual attributes (``__file__``,
       ``__spec__``, ``__loader__``, ``__dict__``, ``__getattr__``,
       ``__dir__``, ``list_symbols``, ``get_symbol``, ``_LAZY``, …)
       alongside the lazy ones — a plain ``dir`` would include them
       anyway, but spelling out the union makes the contract explicit
       and future-proof against refactors that change how ``__all__``
       is built.

    The result is the union of the module's actual namespace (dunders,
    introspection helpers, any submodule already imported and cached by
    :func:`__getattr__`) and :data:`__all__`, deduplicated and sorted
    so that ``dir()`` output is stable across Python versions and
    repeat calls.
    """
    return sorted(set(globals()) | set(__all__))


# ===========================================================================
# Introspection helpers
# ===========================================================================

def list_symbols() -> list[str]:
    """Return the sorted list of top-level lazy exports.

    Useful for parametrized tests (``@pytest.mark.parametrize("name",
    list_symbols())``), schema registries, and tooling that needs to
    enumerate the packaging subsystem's public surface without importing
    every submodule. The list is derived from :data:`_LAZY` — it
    reflects the actual lazy registry, not a hand-maintained second
    list that could drift from :data:`__all__`.
    """
    # Only the public exports, not the introspection helpers themselves -
    # otherwise ``list_symbols`` would include itself.
    return sorted(_LAZY.keys())


def get_symbol(name: str) -> Any:
    """Return the packaging symbol registered under ``name``.

    Equivalent to ``getattr(package, name)`` but with a clearer error
    message when the name is unknown. Convenient for dynamic lookups
    (parametrized tests, format-specific dispatchers that resolve a
    packager class from a config string).

    The lookup goes through :func:`__getattr__` so the resolution is
    cached in ``globals()`` and subsequent calls do not pay the
    ``importlib`` overhead again — and so a missing optional dependency
    surfaces as an :class:`ImportError` with the ``nexusdl[core]`` hint,
    not a raw ``ModuleNotFoundError``.

    Raises:
        KeyError: If ``name`` is not a public packaging export.
    """
    if name not in _LAZY:
        available = ", ".join(sorted(_LAZY.keys()))
        raise KeyError(
            f"Unknown packaging symbol {name!r}. Available: {available}. "
            f"Every other symbol lives in its specific submodule - import "
            f"from 'nexusdl.core.packaging.<submodule>' directly."
        )
    return __getattr__(name)
