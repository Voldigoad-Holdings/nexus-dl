"""Abstract base class for chapter packagers.

Every concrete packager — FOLDER, ZIP, CBZ, CB7, PDF — subclasses
:class:`BasePackager` and implements a single coroutine, :meth:`package`.
The ABC defines the contract those subclasses must satisfy and gives the
:class:`~nexusdl.core.packaging.factory.PackagerFactory` a single type to
dispatch and validate against.

Contract (fixed)
----------------
::

    class BasePackager(ABC):
        format: ClassVar[PackagingFormat]

        @abstractmethod
        async def package(
            self,
            pages: Sequence[tuple[Page, Path]],
            output: Path,
            *,
            metadata: ComicInfo | None = None,
            compression_level: int | None = None,
        ) -> Path: ...

        def supports(self, fmt: PackagingFormat) -> bool: ...

Every concrete packager in this package (and any future one, including
plugin-provided packagers) implements exactly that signature. The worker
(``nexusdl.core.downloader.worker.DownloadWorker._package``) and the
factory both call through it, so a signature drift in any subclass fails
at ``mypy --strict`` time in the subclass's own file — not silently at
runtime in the worker.

What the contract guarantees to the caller
------------------------------------------
* **Return value** — on success, ``package`` returns ``output``
  unchanged. The return is the path the packager *actually* produced,
  which for every packager in this package is the same path the caller
  passed — but a subclass could legitimately adjust it (e.g. append a
  suffix when the caller's target has none). The worker uses the return
  value as the authoritative output path.
* **Atomicity** — ``output`` is either the previous content or the new
  content, never a partially-written artifact. This is a **contract**
  the caller relies on for the resume / retry logic; every concrete
  packager in this package satisfies it (via temp-file-plus-rename or
  temp-dir-plus-swap).
* **Extension validation** — each packager checks that ``output``'s
  extension matches its format (``.zip``, ``.cbz``, ``.cb7``, ``.pdf``,
  or no suffix for ``FOLDER``). A caller that mixes up ``fmt`` and
  ``output`` gets a clear error before any file is written.
* **Metadata handling** — ``metadata`` is honoured by the packagers
  whose format supports it (CBZ, CB7, FOLDER, PDF); ignored by ZIP. The
  packager documents which is which in its own docstring.
* **Compression handling** — ``compression_level`` is honoured by ZIP
  (DEFLATE 0-9, 0=STORED) and CB7 (LZMA2 preset 0-9); ignored by PDF
  (lossless by design) and FOLDER (images are already compressed). The
  ABC does not dictate semantics — each subclass documents them.

What the contract does **not** require
--------------------------------------
* **Instance state.** Packagers are stateless and are constructed with
  no arguments. The factory creates a fresh instance on every call; a
  subclass that needs configuration must accept it via ``__init__`` and
  its factory must be registered with
  :meth:`~nexusdl.core.packaging.factory.PackagerFactory.register`
  (a ``lambda`` closing over the configuration). The default packagers
  in this package deliberately have no ``__init__``.
* **Thread safety of package().** ``package`` is a coroutine; the
  framework calls it from a single event loop. Subclasses may run their
  CPU-bound work via ``asyncio.to_thread``, but they must not assume the
  caller serialises concurrent ``package`` calls on distinct instances.
  Two ``package`` calls on the *same instance* are safe **because** the
  default packagers carry no mutable state — a subclass that introduces
  mutable instance state must document the resulting threading
  constraints.

Enforcement of ``format``
-------------------------
:meth:`__init_subclass__` runs for every subclass and validates that:

* :attr:`format` is defined (either on the subclass or inherited from an
  ancestor that already passed this check),
* :attr:`format` is a :class:`~nexusdl.core.models.enums.PackagingFormat`
  member.

Both are ``TypeError`` at **class definition time** — the class body
executes only when the module is imported, so a subclass that forgot to
set ``format`` fails at import, not at the first ``package`` call.
This is what makes the
:data:`~nexusdl.core.packaging.factory._DEFAULT_REGISTRY` safe to trust:
a typo in ``format = PackagingFormat.CBZ`` becomes an ``AttributeError``
on the *module* import, not a wrong-format archive silently produced at
runtime.

Dependency graph (no cycles)::

    core.models.enums  ─┐
    core.models.page    ├──► core.packaging.base
    core.packaging.comic_info (TYPE_CHECKING only)

The ABC itself imports nothing from the concrete packagers, so a
subclass can import the ABC (which it must) without any risk of an
import cycle.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Final

from nexusdl.core.models.enums import PackagingFormat

if TYPE_CHECKING:
    from nexusdl.core.models.page import Page
    from nexusdl.core.packaging.comic_info import ComicInfo


__all__ = ["BasePackager"]


# --------------------------------------------------------------------------- #
# BasePackager                                                                #
# --------------------------------------------------------------------------- #


class BasePackager(ABC):
    """Abstract base class for chapter packagers.

    See the module docstring for the full contract. Subclasses must:

    1. Set :attr:`format` to the :class:`PackagingFormat` member they
       implement (checked by :meth:`__init_subclass__`).
    2. Override :meth:`package` with the documented signature.

    Subclasses that need configuration accept it in ``__init__``; the
    default packagers in this package do not, and are instantiated with
    no arguments by the
    :class:`~nexusdl.core.packaging.factory.PackagerFactory`.
    """

    #: The format this packager produces. Set by every concrete subclass;
    #: validated at subclass-definition time by :meth:`__init_subclass__`.
    format: ClassVar[PackagingFormat]

    # ------------------------------------------------------------------ #
    # Contract enforcement                                               #
    # ------------------------------------------------------------------ #

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Validate that *cls* declares a valid :attr:`format`.

        Called by Python automatically for every subclass. Fails at
        **class definition time** rather than at first use, so a typo in
        a concrete packager surfaces as an import error rather than as a
        mysterious wrong-format archive produced at runtime.

        Args:
            **kwargs: Forwarded to :meth:`object.__init_subclass__`.

        Raises:
            TypeError: If :attr:`format` is not defined on *cls* (or
                inherited from an ancestor that already passed this
                check), or if its value is not a
                :class:`~nexusdl.core.models.enums.PackagingFormat`.
        """
        super().__init_subclass__(**kwargs)
        # ``hasattr`` correctly returns ``False`` when ``format`` exists
        # only as a ``ClassVar`` annotation (annotations do not create
        # attributes at runtime), so a subclass that forgets to assign
        # ``format = PackagingFormat.X`` is caught here.
        if not hasattr(cls, "format"):
            raise TypeError(
                f"{cls.__qualname__} must define a class-level "
                f"`format` attribute of type PackagingFormat"
            )
        if not isinstance(cls.format, PackagingFormat):
            raise TypeError(
                f"{cls.__qualname__}.format must be a PackagingFormat, "
                f"got {type(cls.format).__name__}"
            )

    # ------------------------------------------------------------------ #
    # Abstract API                                                       #
    # ------------------------------------------------------------------ #

    @abstractmethod
    async def package(
        self,
        pages: Sequence[tuple[Page, Path]],
        output: Path,
        *,
        metadata: ComicInfo | None = None,
        compression_level: int | None = None,
    ) -> Path:
        """Package *pages* into *output* according to the format.

        Concrete implementations are responsible for:

        * validating that *output* carries the extension expected by the
          format (``.zip``, ``.cbz``, ``.cb7``, ``.pdf``, or none for
          ``FOLDER``),
        * validating that every source file in *pages* exists,
        * rejecting duplicate entry names (two pages mapping to the same
          archive entry),
        * honouring or explicitly ignoring *metadata* according to the
          format's convention (documented per packager),
        * honouring or explicitly ignoring *compression_level* according
          to the format's capabilities (documented per packager),
        * writing *output* atomically — the previous contents of *output*
          must survive any failure, including ``asyncio.CancelledError``,
        * cleaning up any temporary files on every exit path.

        Args:
            pages: The chapter's pages, each paired with the file the
                worker downloaded it to. Order is not significant — the
                packager sorts by :attr:`Page.index` before writing.
            output: Destination path. Its parent directory is created
                if it does not exist; an existing file is replaced
                atomically.
            metadata: Optional chapter metadata. Formats that carry a
                ``ComicInfo.xml`` (CBZ, CB7, FOLDER) embed it; PDF maps
                a subset to document properties; ZIP ignores it.
            compression_level: Optional compression setting. ZIP and
                CB7 honour it; PDF and FOLDER ignore it. Semantics are
                documented per packager.

        Returns:
            The path the packager actually produced, which for every
            packager in this package equals *output*.

        Raises:
            PackagingError: On any failure — empty page list, wrong
                output extension, missing source file, duplicate entry
                name, out-of-range compression level, or any I/O step
                failure.
            asyncio.CancelledError: If the surrounding task is cancelled.
                Must propagate untouched (concrete packagers may run
                their work in a worker thread via
                :func:`asyncio.to_thread` and must not swallow the
                cancellation that reaches the coroutine).
        """
        ...

    # ------------------------------------------------------------------ #
    # Concrete API                                                       #
    # ------------------------------------------------------------------ #

    def supports(self, fmt: PackagingFormat) -> bool:
        """Return ``True`` iff this packager can produce *fmt*.

        The default implementation compares *fmt* to :attr:`format`
        exactly — the packagers in this package each handle exactly one
        format. A subclass that genuinely handles multiple formats (which
        none of the shipped ones do, but a plugin might) can override
        this method.

        Args:
            fmt: The format to test.

        Returns:
            ``True`` iff *fmt* is the format this packager produces.
        """
        return fmt == self.format

    def __repr__(self) -> str:
        """Return a concise debug representation.

        Uses the class name and the declared :attr:`format`. Useful in
        logs where a packager instance is printed — a bare
        ``<CbzPackager object at 0x…>`` would be less informative than
        ``CbzPackager(format='cbz')``.
        """
        return f"{type(self).__name__}(format={self.format.value!r})"
