# ════════════════════════════════════════════════════════════════════
# NexusDL — Sphinx configuration
#
# Fichier : docs/source/conf.py
#
# Rôle : configurer Sphinx pour construire la documentation NexusDL.
#
# Extensions utilisées :
#   - myst_parser              → Markdown (MyST)
#   - sphinx.ext.autodoc       → Docstrings Python → doc
#   - sphinx.ext.napoleon      → Google/NumPy docstrings
#   - sphinx.ext.autosummary   → Résumé autodoc
#   - sphinx.ext.viewcode      → Lien [source]
#   - sphinx.ext.intersphinx   → Liens vers docs externes (Python, etc.)
#   - sphinx.ext.todo          → Directives TODO
#   - sphinx.ext.coverage      → Couverture doc
#   - sphinx.ext.linkcode      → Liens "voir sur GitHub"
#   - sphinx_rtd_theme         → Thème Read the Docs
#   - sphinx_copybutton        → Bouton "copier" sur code
#   - sphinx_design            → Cards, tabs, grids
#   - sphinx_autodoc_typehints → Types dans autodoc
#   - sphinx.ext.imgmath       → Maths (images)
#   - sphinx_sitemap           → sitemap.xml (SEO)
#
# Build :
#   just docs           # → _build/html/index.html
#   make -C docs html   # équivalent
#   sphinx-build -b html -W docs/source docs/_build/html   # strict
# ════════════════════════════════════════════════════════════════════

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ════════════════════════════════════════════════════════════════════
# 1. CHEMINS ET IMPORTS
# ════════════════════════════════════════════════════════════════════

# Chemin racine du projet (docs/source/../../)
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent

# Ajouter src/ au sys.path pour autodoc
sys.path.insert(0, str(_REPO_ROOT / "src"))

# Vérifier que nexusdl est importable
try:
    import nexusdl  # noqa: F401
except ImportError as exc:
    _msg = (
        f"Cannot import 'nexusdl' for autodoc. "
        f"Install with: pip install -e '.[docs]'. Error: {exc}"
    )
    raise RuntimeError(_msg) from exc


# ════════════════════════════════════════════════════════════════════
# 2. MÉTADONNÉES DU PROJET
# ════════════════════════════════════════════════════════════════════

project = "NexusDL"
author = "Voldigoad-Holdings"
copyright = f"{datetime.now(tz=timezone.utc).year}, {author}"

# Version (depuis le package)
try:
    from nexusdl.version import __version__ as release  # type: ignore[import-not-found]

    version = ".".join(release.split(".")[:2])  # "0.1" (short version)
except ImportError:
    release = "0.1.0"
    version = "0.1"

# Statut de release (custom, exposé au template)
release_status = "alpha"  # alpha | beta | rc | stable

# Langue
language = "en"

# ════════════════════════════════════════════════════════════════════
# 3. EXTENSIONS
# ════════════════════════════════════════════════════════════════════

extensions = [
    # ── Markdown ──
    "myst_parser",
    # ── Autodoc ──
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx_autodoc_typehints",
    # ── Interop ──
    "sphinx.ext.intersphinx",
    # ── Extras ──
    "sphinx.ext.todo",
    "sphinx.ext.coverage",
    "sphinx.ext.linkcode",
    # ── Theme & UI ──
    "sphinx_rtd_theme",
    "sphinx_copybutton",
    "sphinx_design",
    # ── Maths ──
    "sphinx.ext.imgmath",
    # ── SEO ──
    "sphinx_sitemap",
]

# ════════════════════════════════════════════════════════════════════
# 4. OPTIONS SPHINX CORE
# ════════════════════════════════════════════════════════════════════

templates_path = ["_templates"]
exclude_patterns = [
    "_build",
    "Thumbs.db",
    ".DS_Store",
    ".git",
    "**.ipynb_checkpoints",
]

# Source files (MyST + RST)
source_suffix = {
    ".rst": "restructuredtext",
    ".md": "markdown",
}

# Master document
master_doc = "index"

# Pygments (code highlighting)
pygments_style = "friendly"
pygments_dark_style = "monokai"  # for dark mode

# Suppress warnings from autodoc when optional deps are missing
nitpicky = False
suppress_warnings = [
    "autodoc.mocked_object",
    "autodoc.import_object",
    "ref.myst",
    "misc.highlighting_failure",
]

# Keep going (show all warnings, not just first)
keep_warnings = True

# ════════════════════════════════════════════════════════════════════
# 5. MISTUNE / MyST PARSER
# ════════════════════════════════════════════════════════════════════

myst_enable_extensions = [
    "colon_fence",       # ::: note ::: (Sphinx directives in Markdown)
    "deflist",           # definition lists
    "dollarmath",        # $math$ and $$math$$
    "fieldlist",         # :field: syntax
    "html_image",        # <img> in Markdown
    "linkify",           # auto-link URLs
    "replacements",      # -- → –, ... → …
    "smartquotes",       # " → " and ' → '
    "strikethrough",     # ~~strikethrough~~
    "substitution",      # {{ variable }}
    "tasklist",          # - [ ] and - [x]
]

# Fence prefix (default `{`, but we use both `{` and `::: `)
myst_fence_as_directive = False
myst_heading_anchors = 3  # auto-generate anchors for h1-h3

# ════════════════════════════════════════════════════════════════════
# 6. AUTODOC
# ════════════════════════════════════════════════════════════════════

autodoc_default_options = {
    "members": True,
    "member-order": "bysource",
    "special-members": "__init__",
    "undoc-members": True,
    "exclude-members": "__weakref__,__dict__,__module__,__annotations__",
    "show-inheritance": True,
    "inherited-members": False,
}

autodoc_typehints = "description"  # types in description, not signature
autodoc_typehints_description_target = "documented"
autodoc_typehints_format = "short"  # typing.List → list
autodoc_member_order = "bysource"
autodoc_mock_imports = [
    # Optional deps that may not be installed during docs build
    "customtkinter",
    "playwright",
    "textual",
    "typer",
    "fastapi",
    "uvicorn",
    "jose",
    "passlib",
    "py7zr",
    "img2pdf",
    "selectolax",
    "hishel",
]

# Napoleon (Google/NumPy docstrings)
napoleon_google_docstring = True
napoleon_numpy_docstring = False
napoleon_include_init_with_doc = True
napoleon_include_private_with_doc = False
napoleon_include_special_with_doc = True
napoleon_use_admonition_for_examples = True
napoleon_use_admonition_for_notes = True
napoleon_use_admonition_for_references = True
napoleon_use_ivar = False
napoleon_use_param = True
napoleon_use_rtype = True
napoleon_preprocess_types = True
napoleon_type_aliases = None
napoleon_attr_annotations = True

# Autosummary
autosummary_generate = True
autosummary_imported_members = False

# ════════════════════════════════════════════════════════════════════
# 7. INTERSPHINX (liens vers docs externes)
# ════════════════════════════════════════════════════════════════════

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "pydantic": ("https://docs.pydantic.dev/latest", None),
    "httpx": ("https://www.python-httpx.org", None),
    "fastapi": ("https://fastapi.tiangolo.com", None),
    "sqlalchemy": ("https://docs.sqlalchemy.org/en/20", None),
    "playwright": ("https://playwright.dev/python", None),
    "textual": ("https://textual.textualize.io", None),
    "typer": ("https://typer.tiangolo.com", None),
    "click": ("https://click.palletsprojects.com/en/8.1.x", None),
    "pytest": ("https://docs.pytest.org/en/stable", None),
    "aiohttp": ("https://docs.aiohttp.org/en/stable", None),
    "sphinx": ("https://www.sphinx-doc.org/en/master", None),
}

intersphinx_timeout = 10
intersphinx_disabled_reftypes = ["*"]

# ════════════════════════════════════════════════════════════════════
# 8. VIEWCODE / LINKCODE (liens "voir sur GitHub")
# ════════════════════════════════════════════════════════════════════

viewcode_follow_imported_members = True


def linkcode_resolve(domain: str, info: dict[str, Any]) -> str | None:
    """Résout les liens "source" vers GitHub.

    Args:
        domain: Sphinx domain (e.g. "py").
        info: Dict with "module" and "fullname".

    Returns:
        URL to GitHub source, or None.
    """
    if domain != "py":
        return None

    module_name = info.get("module")
    full_name = info.get("fullname")
    if not module_name or not full_name:
        return None

    try:
        module = sys.modules.get(module_name)
        if module is None:
            return None

        obj = module
        for part in full_name.split("."):
            obj = getattr(obj, part, None)
            if obj is None:
                return None

        source_file = getattr(module, "__file__", None)
        if source_file is None:
            return None

        source_path = Path(source_file).resolve()
        if not source_path.is_file():
            return None

        rel_path = source_path.relative_to(_REPO_ROOT / "src")
        lineno = getattr(obj, "__code__", None)
        lines = f"#L{lineno.co_firstlineno}" if lineno is not None else ""

        return (
            f"https://github.com/Voldigoad-Holdings/nexus-dl"
            f"/blob/main/src/{rel_path.as_posix()}{lines}"
        )
    except (ValueError, AttributeError, OSError):
        return None


# ════════════════════════════════════════════════════════════════════
# 9. THÈME (Read the Docs)
# ════════════════════════════════════════════════════════════════════

html_theme = "sphinx_rtd_theme"
html_theme_path: list[str] = []  # sphinx_rtd_theme is installed, no custom path

html_theme_options = {
    "logo_only": False,
    "display_version": True,
    "prev_next_buttons_location": "bottom",
    "style_external_links": True,
    "vcs_pageview_mode": "",  # we use linkcode instead
    "navigation_depth": 4,
    "collapse_navigation": False,
    "sticky_navigation": True,
    "includehidden": True,
    "titles_only": False,
}

# ════════════════════════════════════════════════════════════════════
# 10. HTML OUTPUT
# ════════════════════════════════════════════════════════════════════

html_title = f"{project} {release} documentation"
html_short_title = project
html_logo = "_static/logo.png"
html_favicon = "_static/logo.png"

html_static_path = ["_static"]
html_css_files = ["custom.css"]
html_js_files: list[str] = []

html_last_updated_fmt = "%Y-%m-%d"
html_use_smartypants = True
html_show_sourcelink = True
html_show_sphinx = True
html_show_copyright = True
html_copy_source = False  # do not copy .rst/.md to _build/html/_sources
html_use_index = True
html_split_index = False
html_sidebars = {
    "**": [
        "about.html",
        "navigation.html",
        "relations.html",
        "searchbox.html",
    ],
}

# Syntax highlighting
html_codeblock_linenos_style = "inline"

# Extra head (meta tags, favicon, etc.) — can be overridden in layout.html
html_context: dict[str, Any] = {
    # ── Project metadata ──
    "project": project,
    "version": version,
    "release": release,
    "release_status": release_status,
    # ── GitHub ──
    "github_url": "https://github.com/Voldigoad-Holdings/nexus-dl",
    "github_user": "Voldigoad-Holdings",
    "github_repo": "nexus-dl",
    "show_source": True,
    # ── SEO ──
    "meta_description": (
        "NexusDL — Multi-site manga, webtoon, comic and doujinshi downloader "
        "with CLI, TUI, GUI and Web interfaces."
    ),
    "meta_author": author,
    # ── Optional (uncomment when assets exist) ──
    # "meta_og_image": "_static/og-image.png",
    # "meta_twitter_site": "@VoldigoadHoldings",
    # ── Theme ──
    "display_github": True,
    "display_gitlab": False,
    "display_bitbucket": False,
}

# ════════════════════════════════════════════════════════════════════
# 11. SEO — sitemap.xml
# ════════════════════════════════════════════════════════════════════

# Set via env var in CI (default to example.com)
html_baseurl = os.environ.get("SPHINX_HTML_BASEURL", "https://nexusdl.example.com/")
sitemap_url_scheme = "{link}"
sitemap_filename = "sitemap.xml"
sitemap_locales = [None]  # no i18n
sitemap_excludes = [
    "search.html",
    "genindex.html",
    "py-modindex.html",
]

# ════════════════════════════════════════════════════════════════════
# 12. LATEX (PDF)
# ════════════════════════════════════════════════════════════════════

latex_documents = [
    (
        master_doc,
        "nexusdl.tex",
        f"{project} Documentation",
        author,
        "manual",
    ),
]

latex_elements = {
    "papersize": "a4paper",
    "pointsize": "10pt",
    "preamble": r"""
        \usepackage{microtype}
        \usepackage{hyperref}
        \hypersetup{
            colorlinks=true,
            linkcolor=NexusIndigo,
            urlcolor=NexusIndigo,
            citecolor=NexusIndigo,
        }
        \definecolor{NexusIndigo}{HTML}{6366F1}
    """,
    "figure_align": "htbp",
}

# ════════════════════════════════════════════════════════════════════
# 13. MAN PAGES
# ════════════════════════════════════════════════════════════════════

man_pages = [
    (master_doc, "nexusdl", f"{project} Documentation", [author], 1),
]

# ════════════════════════════════════════════════════════════════════
# 14. COVERAGE
# ════════════════════════════════════════════════════════════════════

coverage_show_missing_items = True
coverage_ignore_modules = [
    r"nexusdl\.plugins\._example\..*",
    r"nexusdl\.parsers\.custom\._template",
]
coverage_ignore_functions = [
    r"nexusdl\..*\.__repr__",
]
coverage_ignore_classes = [
    r"nexusdl\.parsers\..*Parser$",  # parsers are documented in their own page
]

# ════════════════════════════════════════════════════════════════════
# 15. TODO
# ════════════════════════════════════════════════════════════════════

todo_include_todos = True
todo_emit_warnings = False
todo_link_only = False

# ════════════════════════════════════════════════════════════════════
# 16. NAPOLEON / GOOGLE STYLE — ALIASES
# ════════════════════════════════════════════════════════════════════

# Map short type names to long ones (for cleaner docs)
autodoc_type_aliases = {
    # Core models
    "PathLike": "os.PathLike",
    "HttpUrl": "pydantic.HttpUrl",
    "UUID": "uuid.UUID",
    # NexusDL types
    "DownloadStatus": "nexusdl.core.models.enums.DownloadStatus",
    "MangaStatus": "nexusdl.core.models.enums.MangaStatus",
    "PackagingFormat": "nexusdl.core.models.enums.PackagingFormat",
    "Language": "nexusdl.core.models.enums.Language",
    "ContentRating": "nexusdl.core.models.enums.ContentRating",
    "Priority": "nexusdl.core.models.enums.Priority",
    "UserRole": "nexusdl.core.models.enums.UserRole",
    "SiteHealth": "nexusdl.core.models.enums.SiteHealth",
}

# ════════════════════════════════════════════════════════════════════
# 17. BUILD HOOKS
# ════════════════════════════════════════════════════════════════════


def setup(app: Any) -> dict[str, Any]:
    """Sphinx extension setup.

    Args:
        app: Sphinx application.

    Returns:
        Extension metadata.
    """

    def _add_custom_css(app: Any) -> None:
        # Ensure custom.css is loaded even if `html_css_files` misses it
        app.add_css_file("custom.css")

    def _add_robots_noindex(app: Any) -> None:
        # Add a meta tag if not in prod (release_status != stable)
        if release_status != "stable":
            app.add_meta_tag = None  # placeholder, real impl in layout.html

    app.connect("builder-inited", _add_custom_css)

    return {
        "version": "1.0",
        "parallel_read_safe": True,
        "parallel_write_safe": True,
    }
