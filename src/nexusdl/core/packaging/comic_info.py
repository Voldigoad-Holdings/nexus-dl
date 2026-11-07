"""ComicInfo.xml model and XML serialization.

``ComicInfo.xml`` is the de-facto metadata standard for manga and comic
archives. It was defined by ComicRack (now unmaintained) and is now read
by every modern reader: **Komga**, **Kavita**, **Mihon** (Tachiyomi's
successor), **YACReader**, and **CDisplayEx**. The file lives at the root
of a ``.cbz`` / ``.cb7`` archive, or inside a folder-packaged chapter
directory.

This module defines:

* :class:`ComicInfo` — the full ComicRack + Anansi field set as a
  Pydantic v2 model. Every reader in the ecosystem is a subset of this
  schema, so encoding once here is enough.
* :class:`ComicInfoPage` — the ``<Pages><Page .../></Pages>`` block, which
  describes each page's dimensions and file name.
* :meth:`ComicInfo.to_xml` — serialize to a UTF-8 string.
* :meth:`ComicInfo.from_xml` — parse from a UTF-8 string, **tolerating
  unknown fields** so files produced by other tools (a ComicRack
  export, a Mylar3 import, a hand-edited file) round-trip without loss.

Compatibility strategy
----------------------
Two design decisions make the model forgiving by default:

* **Extra fields are ignored** (``extra="ignore"``). ComicRack and its
  forks add fields that no reader actually uses (``StoryArc``,
  ``SeriesGroup``, ``AlternateSeries``, …). Rejecting them would make
  NexusDL unable to read archives produced by any other tool. This is
  the **opposite** choice from :class:`~nexusdl.plugins.validators.PluginManifest`,
  which uses ``extra="forbid"`` — because that schema describes a config
  file NexusDL itself wrote, while this schema describes a file that
  many tools may have written.
* **XML parsing is namespace-tolerant.** Some tools write
  ``<ComicInfo xmlns:xsi="...">``; ``xml.etree.ElementTree`` keeps the
  namespace in ``element.tag``. Both serialization and parsing here use
  :func:`_local_name` to strip any namespace prefix from tags, so
  ``{http://www.w3.org/2001/XMLSchema-instance}Title`` and ``Title`` are
  treated identically.

Required vs optional fields
---------------------------
Per the PROMPT MASTER, only **five** fields are required:

* :attr:`ComicInfo.title`
* :attr:`ComicInfo.series`
* :attr:`ComicInfo.number`
* :attr:`ComicInfo.language_iso`
* :attr:`ComicInfo.page_count`

Every other field defaults to ``None`` (scalars) or ``()`` (collections)
and is **omitted** from the XML when unset. The current call sites
(``worker._build_comic_info`` and ``metadata._synthesize_comic_info``)
provide those five always, so no caller has to remember to fill them.

``Manga`` and ``BlackAndWhite`` are strings, not booleans
---------------------------------------------------------
The ComicRack schema declares these as the strings ``"Yes"``, ``"No"``,
``"YesAndRightToLeft"``, and ``"NoAndRightToLeft"`` — an unfortunate
design, but one that every reader relies on. The model therefore types
them as :class:`str`, not :class:`bool`, and the conversion layer
(:func:`nexusdl.core.library.metadata._is_manga`) interprets the string.
The worker emits ``"YesAndRightToLeft"`` or ``"No"`` directly.

CSV-encoded list fields
-----------------------
The ComicRack schema encodes ``Genre``, ``Tags``, ``Characters``,
``Teams``, and ``Locations`` as single comma-separated strings, not as
repeated elements. This model exposes them as tuples of strings and
handles the CSV round-trip internally:

* Write — ``", ".join(items)``.
* Read — ``[part.strip() for part in value.split(",") if part.strip()]``.

An empty tuple serializes to an absent element (not ``<Genre></Genre>``).
This is both correct (the field is "not set") and what readers expect.

Byte-order-mark and encoding
----------------------------
Writing uses UTF-8 **without** a BOM. Reading accepts a UTF-8 BOM if
present (``ElementTree.fromstring`` strips it), because some Windows
tools (notably older versions of Comical) emit a BOM that other readers
tolerate. Handling both directions costs one line and prevents a class
of "the file has a BOM, the parser chokes" bug reports.

Dependency graph (no cycles)::

    pydantic  ──► core.packaging.comic_info

No dependency on any other ``nexusdl.core`` module — this file could be
lifted into a standalone library together with Pydantic.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator


__all__ = ["ComicInfo", "ComicInfoPage"]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Root element tag. ComicRack defines exactly this spelling, in title
#: case, with no namespace.
_ROOT_TAG: Final[str] = "ComicInfo"

#: XML declaration prepended to the serialized output. ``ElementTree``
#: emits the same declaration (with single quotes), but including it
#: explicitly in the returned string keeps the output deterministic
#: regardless of the Python version's ``ElementTree`` quoting style.
_XML_DECLARATION: Final[str] = '<?xml version="1.0" encoding="utf-8"?>'

#: Fields whose value is a comma-separated string in the XML schema.
#: Mapped from the model's tuple fields to the corresponding element name.
_CSV_FIELDS: Final[tuple[tuple[str, str], ...]] = (
    ("genres", "Genre"),
    ("tags", "Tags"),
    ("characters", "Characters"),
    ("teams", "Teams"),
    ("locations", "Locations"),
)

#: Scalar fields in canonical XML order. ``None`` values are omitted from
#: the output. The order follows the ComicRack documentation and matches
#: what every reader expects; it is not semantically significant but
#: keeping it stable makes the output diff-friendly.
_SCALAR_FIELDS: Final[tuple[tuple[str, str], ...]] = (
    ("title", "Title"),
    ("series", "Series"),
    ("number", "Number"),
    ("count", "Count"),
    ("volume", "Volume"),
    ("alternate_series", "AlternateSeries"),
    ("alternate_number", "AlternateNumber"),
    ("alternate_count", "AlternateCount"),
    ("summary", "Summary"),
    ("notes", "Notes"),
    ("year", "Year"),
    ("month", "Month"),
    ("day", "Day"),
    ("writer", "Writer"),
    ("penciller", "Penciller"),
    ("inker", "Inker"),
    ("colorist", "Colorist"),
    ("letterer", "Letterer"),
    ("cover_artist", "CoverArtist"),
    ("editor", "Editor"),
    ("translator", "Translator"),
    ("publisher", "Publisher"),
    ("imprint", "Imprint"),
    ("web", "Web"),
    ("page_count", "PageCount"),
    ("language_iso", "LanguageISO"),
    ("format", "Format"),
    ("black_and_white", "BlackAndWhite"),
    ("manga", "Manga"),
    ("scan_information", "ScanInformation"),
    ("story_arc", "StoryArc"),
    ("series_group", "SeriesGroup"),
    ("age_rating", "AgeRating"),
)


# --------------------------------------------------------------------------- #
# ComicInfoPage                                                               #
# --------------------------------------------------------------------------- #


class ComicInfoPage(BaseModel):
    """One ``<Page .../>`` entry inside ``<Pages>``.

    The ComicRack schema encodes page metadata as **attributes** on the
    ``<Page>`` element — not as child elements. This model mirrors that
    shape exactly: every field becomes an XML attribute.

    All fields are optional because real-world archives vary widely in
    what they include. The minimal entry that a reader can display is an
    empty ``<Page/>``.

    Attributes:
        image: The page's filename inside the archive (e.g.
            ``"0001.jpg"``). ComicRack's convention is the relative path
            from the archive root; for flat archives, just the basename.
        image_size: File size in bytes.
        image_width: Width in pixels.
        image_height: Height in pixels.
        type: Page type — ComicRack's enumerated values are ``"FrontCover"``,
            ``"InnerCover"``, ``"Roundup"``, ``"Story"``, ``"Advertisement"``,
            ``"Editorial"``, ``"Letters"``, ``"Preview"``, ``"BackCover"``,
            ``"Other"``, ``"Deleted"``. Stored as a free string; no reader
            enforces the enum strictly.
        double_page: ``"Yes"`` / ``"No"`` / ``"Unknown"`` — indicates a
            double-page spread that should be displayed on its own.
    """

    model_config = ConfigDict(
        extra="ignore",
        frozen=True,
        str_strip_whitespace=False,
    )

    image: str | None = None
    image_size: int | None = Field(default=None, ge=0)
    image_width: int | None = Field(default=None, ge=1)
    image_height: int | None = Field(default=None, ge=1)
    type: str | None = None
    double_page: str | None = None


# --------------------------------------------------------------------------- #
# ComicInfo                                                                   #
# --------------------------------------------------------------------------- #


class ComicInfo(BaseModel):
    """The full ``ComicInfo.xml`` schema.

    See the module docstring for the compatibility strategy and the
    required-vs-optional split. Instances are immutable (``frozen=True``);
    use :meth:`to_xml` / :meth:`from_xml` for round-trip, and construct a
    fresh instance if you need a modified copy.

    Field descriptions are grouped by concern. The XML element name for
    each field is shown in parentheses; ``None`` values are omitted from
    the serialized output entirely.
    """

    model_config = ConfigDict(
        extra="ignore",
        frozen=True,
        str_strip_whitespace=False,
    )

    # --- Required ------------------------------------------------------- #
    # These five fields are the only ones the model enforces. Every other
    # call site in the pipeline fills them unconditionally.
    title: str
    series: str
    number: str
    language_iso: str
    page_count: int = Field(ge=0)

    # --- Identification (optional) ------------------------------------ #
    count: int | None = Field(default=None, ge=0)
    volume: str | None = None
    alternate_series: str | None = None
    alternate_number: str | None = None
    alternate_count: int | None = Field(default=None, ge=0)

    # --- Descriptive -------------------------------------------------- #
    summary: str | None = None
    notes: str | None = None
    year: int | None = Field(default=None, ge=0, le=9999)
    month: int | None = Field(default=None, ge=1, le=12)
    day: int | None = Field(default=None, ge=1, le=31)

    # --- Credits ------------------------------------------------------ #
    writer: str | None = None
    penciller: str | None = None
    inker: str | None = None
    colorist: str | None = None
    letterer: str | None = None
    cover_artist: str | None = None
    editor: str | None = None
    translator: str | None = None

    # --- Publication -------------------------------------------------- #
    publisher: str | None = None
    imprint: str | None = None
    web: str | None = None
    format: str | None = None
    age_rating: str | None = None

    # --- Classification (CSV-encoded in XML) ------------------------- #
    genres: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    characters: tuple[str, ...] = ()
    teams: tuple[str, ...] = ()
    locations: tuple[str, ...] = ()

    # --- Reader hints ------------------------------------------------- #
    # ``"Yes"`` / ``"No"`` / ``"YesAndRightToLeft"`` / ``"NoAndRightToLeft"``
    # — see the module docstring for why these are strings.
    manga: str | None = None
    black_and_white: str | None = None

    # --- Provenance --------------------------------------------------- #
    scan_information: str | None = None
    story_arc: str | None = None
    series_group: str | None = None

    # --- Pages -------------------------------------------------------- #
    pages: tuple[ComicInfoPage, ...] = ()

    # ------------------------------------------------------------------ #
    # Validators                                                         #
    # ------------------------------------------------------------------ #

    @field_validator(
        "genres", "tags", "characters", "teams", "locations", mode="before"
    )
    @classmethod
    def _coerce_to_tuple(
        cls, value: Any
    ) -> tuple[str, ...]:
        """Accept a list, a tuple, or ``None`` and normalise to a tuple.

        Pydantic would accept ``list`` directly for ``tuple[str, ...]``
        fields, but we normalise here so that the stored value is
        genuinely immutable, matching the ``frozen=True`` declaration on
        the model.
        """
        if value is None:
            return ()
        if isinstance(value, str):
            # A caller that passed a bare string almost certainly meant a
            # single-element list. Treat it as such — this matches how
            # ComicInfo's CSV encoding behaves when a field has one item.
            return (value,)
        return tuple(value)

    # ------------------------------------------------------------------ #
    # Serialization                                                      #
    # ------------------------------------------------------------------ #

    def to_xml(self) -> str:
        """Serialize to a UTF-8 ``ComicInfo.xml`` string.

        The output is a complete XML document: XML declaration, root
        element, and children in canonical ComicRack order. ``None``
        values are omitted; empty collections are omitted. The output is
        guaranteed to be valid UTF-8 (no BOM) and to parse cleanly with
        :meth:`from_xml`.

        Returns:
            A string ready to be written to disk or returned in an HTTP
            response.
        """
        root = ET.Element(_ROOT_TAG)

        # Scalar fields, in canonical order.
        for model_attr, xml_tag in _SCALAR_FIELDS:
            value = getattr(self, model_attr)
            if value is None:
                continue
            element = ET.SubElement(root, xml_tag)
            element.text = _stringify(value)

        # CSV-encoded collections.
        for model_attr, xml_tag in _CSV_FIELDS:
            items = getattr(self, model_attr)
            if not items:
                continue
            element = ET.SubElement(root, xml_tag)
            element.text = ", ".join(items)

        # Pages block, when populated.
        if self.pages:
            pages_root = ET.SubElement(root, "Pages")
            for page in self.pages:
                ET.SubElement(
                    pages_root,
                    "Page",
                    attrib=_page_attributes(page),
                )

        # Indent for readability. ``ET.indent`` is idempotent-safe here
        # because ``root`` was just constructed in this method and is not
        # shared with any caller.
        ET.indent(root, space="  ")

        body = ET.tostring(root, encoding="unicode")
        return f"{_XML_DECLARATION}\n{body}"

    # ------------------------------------------------------------------ #
    # Deserialization                                                    #
    # ------------------------------------------------------------------ #

    @classmethod
    def from_xml(cls, xml: str) -> ComicInfo:
        """Parse a ``ComicInfo.xml`` string into a :class:`ComicInfo`.

        Tolerant of namespaces (see the module docstring) and of unknown
        elements (they are ignored). Missing required fields raise
        :class:`pydantic.ValidationError`.

        Args:
            xml: The raw XML string. UTF-8 is assumed — a leading
                UTF-8 BOM, if present, is stripped by ``ElementTree``.

        Returns:
            A :class:`ComicInfo` instance.

        Raises:
            ValueError: If *xml* is not parseable as XML, or the root
                element is not ``<ComicInfo>``.
            pydantic.ValidationError: If a required field is missing or a
                field's value is of the wrong type.
        """
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as exc:
            raise ValueError(
                f"cannot parse ComicInfo.xml: {exc}"
            ) from exc

        if _local_name(root.tag) != _ROOT_TAG:
            raise ValueError(
                f"unexpected root element {_local_name(root.tag)!r}; "
                f"expected {_ROOT_TAG!r}"
            )

        scalar_map = {
            xml_tag: model_attr
            for model_attr, xml_tag in _SCALAR_FIELDS
        }
        csv_map = {
            xml_tag: model_attr
            for model_attr, xml_tag in _CSV_FIELDS
        }

        kwargs: dict[str, Any] = {}
        pages: list[ComicInfoPage] = []

        for child in root:
            tag = _local_name(child.tag)

            if tag in scalar_map:
                model_attr = scalar_map[tag]
                # ``to_xml`` produces text-only elements; a child with no
                # text is an explicit empty value. Treat it as ``None``
                # for scalar fields — the alternative (empty string) is
                # not what any reader expects.
                if child.text is None:
                    continue
                kwargs[model_attr] = child.text
                continue

            if tag in csv_map:
                model_attr = csv_map[tag]
                if child.text is None:
                    kwargs[model_attr] = ()
                else:
                    kwargs[model_attr] = _split_csv(child.text)
                continue

            if tag == "Pages":
                pages = _parse_pages(child)
                continue

            # Unknown element — ignore (module docstring explains why).

        if pages:
            kwargs["pages"] = tuple(pages)

        # ``age_rating`` is stored as a string in our model but appears
        # as an integer in some ComicInfo producers. ``from_xml`` reads
        # it verbatim (string) — the model does not attempt to coerce
        # because ComicRack's own field can be one of the enumerated
        # strings or an integer, and either is valid.
        return cls(**kwargs)


# --------------------------------------------------------------------------- #
# Internal helpers                                                           #
# --------------------------------------------------------------------------- #


def _local_name(tag: str) -> str:
    """Return *tag* with any XML namespace prefix stripped.

    ``ElementTree`` exposes namespaced tags as
    ``"{http://www.w3.org/2001/XMLSchema-instance}Title"``. This function
    returns ``"Title"`` regardless of the namespace in use — ComicInfo
    consumers never care which namespace a file was written with.

    Args:
        tag: An ElementTree tag string.

    Returns:
        The local name, without any ``{namespace}`` prefix.
    """
    # ``rsplit("}", 1)[-1]`` is safe even when no ``}`` is present: it
    # returns the original string unchanged.
    return tag.rsplit("}", 1)[-1]


def _stringify(value: Any) -> str:
    """Convert a scalar model field to its XML text representation.

    ``str`` and ``int`` need no special-casing. ``bool`` (if a future
    field is added) must be mapped to the ComicInfo vocabulary —
    ``"Yes"`` / ``"No"`` — because ``str(True)`` produces ``"True"``,
    which no reader recognises. The model deliberately does not use
    ``bool`` today; this helper is written to stay correct if that ever
    changes.

    Args:
        value: A non-``None`` scalar from the model.

    Returns:
        The string to write as the element's text.
    """
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def _page_attributes(page: ComicInfoPage) -> dict[str, str]:
    """Build the XML attribute dict for a single ``<Page>`` element.

    Every non-``None`` field becomes an attribute named after the
    ComicRack convention (``Image``, ``ImageSize``, ``ImageWidth``,
    ``ImageHeight``, ``Type``, ``DoublePage``). Attribute order follows
    the model's field order — deterministic and easy to diff.

    Args:
        page: The page model instance.

    Returns:
        A mapping of attribute name → string value.
    """
    mapping: tuple[tuple[str, str], ...] = (
        ("image", "Image"),
        ("image_size", "ImageSize"),
        ("image_width", "ImageWidth"),
        ("image_height", "ImageHeight"),
        ("type", "Type"),
        ("double_page", "DoublePage"),
    )
    attrs: dict[str, str] = {}
    for model_attr, xml_attr in mapping:
        value = getattr(page, model_attr)
        if value is not None:
            attrs[xml_attr] = str(value)
    return attrs


def _split_csv(value: str) -> tuple[str, ...]:
    """Split a CSV-encoded field back into a tuple of strings.

    Strips whitespace around each item and drops empty items — the same
    normalisation every ComicInfo reader applies.

    Args:
        value: The raw text of a CSV element.

    Returns:
        A tuple of non-empty, stripped strings. Empty when the input has
        no items.
    """
    return tuple(
        part.strip() for part in value.split(",") if part.strip()
    )


def _parse_pages(pages_element: ET.Element) -> list[ComicInfoPage]:
    """Parse a ``<Pages>`` element into a list of :class:`ComicInfoPage`.

    Each ``<Page .../>`` child is turned into a :class:`ComicInfoPage` by
    mapping the ComicRack attribute names back to model field names. A
    ``<Page>`` with no attributes becomes an empty :class:`ComicInfoPage`,
    which is valid.

    Args:
        pages_element: The parent ``<Pages>`` element.

    Returns:
        A list of page models, in document order.
    """
    mapping: dict[str, str] = {
        "Image": "image",
        "ImageSize": "image_size",
        "ImageWidth": "image_width",
        "ImageHeight": "image_height",
        "Type": "type",
        "DoublePage": "double_page",
    }
    pages: list[ComicInfoPage] = []
    for child in pages_element:
        if _local_name(child.tag) != "Page":
            continue
        kwargs: dict[str, Any] = {}
        for xml_attr, model_attr in mapping.items():
            raw = child.attrib.get(xml_attr)
            if raw is not None:
                kwargs[model_attr] = raw
        try:
            pages.append(ComicInfoPage(**kwargs))
        except Exception as exc:  # noqa: BLE001 — surface the exact page
            raise ValueError(
                f"invalid <Page> element: {exc}"
            ) from exc
    return pages
