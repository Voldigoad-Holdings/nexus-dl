"""Public API of the NexusDL packaging subsystem.

The :mod:`nexusdl.core.packaging` package turns a chapter's downloaded
page files into the final artifact the user keeps: a folder, a ZIP, a
CBZ, a CB7, or a PDF. It is the last stage of the download pipeline,
called by :meth:`~nexusdl.core.downloader.worker.DownloadWorker._package`
after all pages have been fetched, validated, and written to a temporary
directory.

Nine modules cooperate, each with a single responsibility:

* :mod:`~nexusdl.core.packaging.base` — the abstract
  :class:`~nexusdl.core.packaging.base.BasePackager` contract every
  concrete packager implements.
* :mod:`~nexusdl.core.packaging.comic_info` — the
  :class:`~nexusdl.core.packaging.comic_info.ComicInfo` model and its
  XML serialization (ComicRack schema, read by Komga, Kavita, Mihon,
  YACReader, CDisplayEx).
* :mod:`~nexusdl.core.packaging.folder_packager` — writes a plain
  directory of images, with an optional ``ComicInfo.xml`` at the root.
* :mod:`~nexusdl.core.packaging.zip_packager` — writes a plain ``.zip``;
  metadata is intentionally ignored (ZIP has no ComicInfo convention).
* :mod:`~nexusdl.core.packaging.cbz_packager` — writes a ``.cbz``
  (ZIP + ``ComicInfo.xml`` when metadata is provided).
* :mod:`~nexusdl.core.packaging.cb7_packager` — writes a ``.cb7``
  (7-Zip + ``ComicInfo.xml``); smaller archives, slower to produce.
* :mod:`~nexusdl.core.packaging.pdf_packager` — writes a single lossless
  PDF via :mod:`img2pdf`; no re-encoding of JPEG/PNG.
* :mod:`~nexusdl.core.packaging.factory` — resolves a
  :class:`~nexusdl.core.models.enums.PackagingFormat` to a concrete
  packager, with lazy imports so the factory is importable even in a
  partial source tree.
* :mod:`~nexusdl.core.packaging.zip_packager` — see above.

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
``CBT``                        **no packager ships** — see ADR-002
============================  ======================================

``CBT`` is declared in
:class:`~nexusdl.core.models.enums.PackagingFormat` for forward
compatibility but deliberately absent from this package's defaults.
:meth:`PackagerFactory.create` on ``CBT`` raises a ``PackagingError``
that names the missing implementation and the supported formats.
Plugins that provide a ``CbtPackager`` can wire it in with
:meth:`PackagerFactory.register`. ``CBR`` is not even declared: see the
note in :mod:`~nexusdl.core.models.enums`.

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

    # Later, if the user asks for CBT and a plugin provides one:
    # packager_factory.register(PackagingFormat.CBT, CbtPackager)

Metadata is optional throughout. ``ZipPackager`` ignores it (the format
has no ComicInfo convention). ``FolderPackager``, ``CbzPackager``,
``Cb7Packager`` embed the serialized ``ComicInfo.xml`` at the artifact
root. ``PdfPackager`` maps a subset of the fields to PDF document
properties. Each packager documents its own choice in the class-level
docstring.

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
  ``_DEFAULT_REGISTRY``, ``_PACKAGE_ROOT``, ``_DECLARED_BUT_UNSHIPPED`` —
  module-private constants.
* ``_resolve_compression``, ``_resolve_preset``, ``_serialize_metadata``,
  ``_fsync_and_replace``, ``_atomic_replace_directory``,
  ``_fsync_directory``, ``_write_comic_info``, ``_write_pdf``,
  ``_convert_pages_to_png``, ``_flatten_to_png``,
  ``_flatten_alpha_to_png``, ``_save_rgb_or_l_to_png``,
  ``_build_metadata_kwargs``, ``_build_pdf_creation_date``,
  ``_local_name``, ``_stringify``, ``_page_attributes``,
  ``_split_csv``, ``_parse_pages``, ``_make_lazy_loader``,
  ``_missing_format_message`` — helper functions.
* ``_validate_manga``, ``_validate_black_and_white``,
  ``_coerce_to_tuple`` on :class:`ComicInfo` — Pydantic field validators
  (invoked automatically by Pydantic, never called directly).

Dependency graph (no cycles)::

    core.exceptions
         ▲
         │
    ┌────┴─────────────────────────────────────────────┐
    │                                                   │
    base ◄── packagers (folder / zip / cbz / cb7 / pdf)│
      ▲                         ▲                       │
      │                         │                       │
      └───── comic_info ────────┘                       │
                 ▲                                       │
                 │                                       │
              factory (imports base eagerly,            │
                       packagers lazily)                │
                                                         │
    ─────────────────────────────────────────────────────┘
                (no import edge to parsers/, plugins/,
                 or interfaces/ — rule R1)

``base`` imports nothing from the concrete packagers; each concrete
packager imports ``base`` and optionally ``comic_info``; ``factory``
imports ``base`` eagerly and resolves each concrete packager lazily at
:meth:`~nexusdl.core.packaging.factory.PackagerFactory.create` time.
"""

from __future__ import annotations

# --- Base contract (imported by every packager) --------------------------- #
from nexusdl.core.packaging.base import BasePackager

# --- ComicInfo (metadata model + XML serialization) ---------------------- #
from nexusdl.core.packaging.comic_info import ComicInfo, ComicInfoPage

# --- Concrete packagers -------------------------------------------------- #
from nexusdl.core.packaging.cb7_packager import Cb7Packager
from nexusdl.core.packaging.cbz_packager import CbzPackager
from nexusdl.core.packaging.folder_packager import FolderPackager
from nexusdl.core.packaging.pdf_packager import PdfPackager
from nexusdl.core.packaging.zip_packager import ZipPackager

# --- Factory (resolves a PackagingFormat to a packager) ------------------ #
from nexusdl.core.packaging.factory import PackagerFactory

__all__ = [
    # --- Base contract -------------------------------------------------- #
    "BasePackager",
    # --- Metadata ------------------------------------------------------- #
    "ComicInfo",
    "ComicInfoPage",
    # --- Concrete packagers -------------------------------------------- #
    "Cb7Packager",
    "CbzPackager",
    "FolderPackager",
    "PdfPackager",
    "ZipPackager",
    # --- Factory -------------------------------------------------------- #
    "PackagerFactory",
]
