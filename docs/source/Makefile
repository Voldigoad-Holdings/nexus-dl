# ════════════════════════════════════════════════════════════════════
# NexusDL — Sphinx documentation Makefile
#
# Fichier : docs/source/Makefile
#
# Rôle : générer la documentation Sphinx à partir de `docs/source/`.
#
# ⚠️  Ce Makefile est SPÉCIFIQUE à Sphinx. Il ne remplace PAS le
#     `Makefile` racine (qui orchestre tout le projet : tests, lint,
#     build, docker, etc.).
#
# Usage :
#   cd docs && make html        # génère docs/_build/html/
#   cd docs && make serve       # http://localhost:8001
#   cd docs && make clean       # supprime docs/_build/
#   cd docs && make linkcheck   # vérifie les liens externes
#   cd docs && make strict      # warnings = errors
#
# Ou depuis la racine :
#   just docs           # équivalent à `make -C docs html`
#   just docs-serve     # équivalent à `make -C docs serve`
#   just docs-clean     # équivalent à `make -C docs clean`
#
# ⚠️  Sphinx et les extensions sont installés via `pip install -e .[docs]`
#     (voir pyproject.toml).
# ════════════════════════════════════════════════════════════════════

# ────────────────────────────────────────────────────────────────────
# Configuration
# ────────────────────────────────────────────────────────────────────

# Répertoires (relatifs à `docs/` — le Makefile est dans `docs/source/`)
SOURCEDIR     := $(CURDIR)/source
BUILDDIR      := $(CURDIR)/_build
PDFBUILDDIR   := $(BUILDDIR)/pdf
DOCTREESDIR   := $(BUILDDIR)/doctrees
SPHINXOPTS    ?=
SPHINXBUILD   ?= sphinx-build
SPHINXAUTOBUILD ?= sphinx-autobuild

# Port par défaut pour `make serve` et `make autobuild`
PORT          ?= 8001

# Couleurs (désactivées si NO_COLOR est défini)
ifdef NO_COLOR
    C_RESET :=
    C_BOLD  :=
    C_CYAN  :=
    C_GREEN :=
    C_YELL  :=
    C_RED   :=
else
    C_RESET := \033[0m
    C_BOLD  := \033[1m
    C_CYAN  := \033[36m
    C_GREEN := \033[32m
    C_YELL  := \033[33m
    C_RED   := \033[31m
endif

# Détection de l'OS pour ouvrir le navigateur
UNAME_S := $(shell uname -s 2>/dev/null || echo "Unknown")
ifeq ($(UNAME_S),Darwin)
    OPEN_CMD := open
else ifeq ($(UNAME_S),Linux)
    OPEN_CMD := xdg-open
else
    OPEN_CMD := start
endif

# Support Windows : `make` sous Git Bash
ifeq ($(OS),Windows_NT)
    OPEN_CMD := start
    # Sur Windows, `make` peut interpréter `\` différemment
    SHELL := /bin/bash
endif

# ────────────────────────────────────────────────────────────────────
# Cible par défaut : affiche l'aide
# ────────────────────────────────────────────────────────────────────

.DEFAULT_GOAL := help

.PHONY: help Makefile \
        html dirhtml singlehtml pickle json htmlhelp qthelp devhelp \
        epub latex latexpdf latexpdfja text man texinfo info gettext \
        changes linkcheck doctest coverage dummy \
        clean clean-all \
        serve autobuild strict \
        all

# ────────────────────────────────────────────────────────────────────
# Aide
# ────────────────────────────────────────────────────────────────────

help:
	@printf "$(C_BOLD)NexusDL — Documentation Makefile$(C_RESET)\n\n"
	@printf "$(C_BOLD)Usage:$(C_RESET)\n"
	@printf "  make $(C_CYAN)<target>$(C_RESET)\n\n"
	@printf "$(C_BOLD)Targets:$(C_RESET)\n"
	@printf "  $(C_CYAN)html$(C_RESET)        Build HTML docs (default)\n"
	@printf "  $(C_CYAN)strict$(C_RESET)      Build HTML with -W (warnings = errors)\n"
	@printf "  $(C_CYAN)serve$(C_RESET)       Build + serve on http://localhost:%s\n" "$(PORT)"
	@printf "  $(C_CYAN)autobuild$(C_RESET)   Build + auto-rebuild + serve (live reload)\n"
	@printf "  $(C_CYAN)linkcheck$(C_RESET)   Check external links (slow)\n"
	@printf "  $(C_CYAN)clean$(C_RESET)       Remove _build/\n"
	@printf "  $(C_CYAN)clean-all$(C_RESET)   Remove _build/ and __pycache__\n"
	@printf "  $(C_CYAN)pdf$(C_RESET)         Build PDF (requires LaTeX)\n"
	@printf "  $(C_CYAN)epub$(C_RESET)        Build EPUB\n"
	@printf "  $(C_CYAN)help$(C_RESET)        Show this help\n"
	@printf "\n$(C_BOLD)Environment:$(C_RESET)\n"
	@printf "  Sphinx:    $(SPHINXBUILD)\n"
	@printf "  Version:   %s\n" "$$($(SPHINXBUILD) --version 2>/dev/null | head -1 || echo 'not installed')"
	@printf "  Source:    $(SOURCEDIR)\n"
	@printf "  Build:     $(BUILDDIR)\n"

# ────────────────────────────────────────────────────────────────────
# Build HTML
# ────────────────────────────────────────────────────────────────────

html:
	@printf "$(C_CYAN)→$(C_RESET) Building HTML documentation...\n"
	@$(SPHINXBUILD) -M html "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)
	@printf "$(C_GREEN)✓$(C_RESET) Built: $(BUILDDIR)/html/index.html\n"

strict:
	@printf "$(C_CYAN)→$(C_RESET) Building HTML (strict mode, warnings = errors)...\n"
	@$(SPHINXBUILD) -M html "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O) -W --keep-going
	@printf "$(C_GREEN)✓$(C_RESET) Built: $(BUILDDIR)/html/index.html\n"

dirhtml:
	@$(SPHINXBUILD) -M dirhtml "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)

singlehtml:
	@$(SPHINXBUILD) -M singlehtml "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)

# ────────────────────────────────────────────────────────────────────
# Serve (HTTP + navigateur)
# ────────────────────────────────────────────────────────────────────

serve: html
	@printf "$(C_CYAN)→$(C_RESET) Serving on $(C_BOLD)http://localhost:%s$(C_RESET)\n" "$(PORT)"
	@printf "  Ctrl+C to stop.\n\n"
	@$(PYTHON) -m http.server $(PORT) --directory "$(BUILDDIR)/html" --bind 127.0.0.1

# ────────────────────────────────────────────────────────────────────
# Autobuild (live reload)
# ────────────────────────────────────────────────────────────────────

autobuild:
	@printf "$(C_CYAN)→$(C_RESET) Auto-building with live reload...\n"
	@printf "  $(C_BOLD)http://127.0.0.1:%s$(C_RESET)\n\n" "$(PORT)"
	@$(SPHINXAUTOBUILD) --port $(PORT) --open-browser "$(SOURCEDIR)" "$(BUILDDIR)/html"

# ────────────────────────────────────────────────────────────────────
# Link check
# ────────────────────────────────────────────────────────────────────

linkcheck:
	@printf "$(C_CYAN)→$(C_RESET) Checking external links (slow)...\n"
	@$(SPHINXBUILD) -M linkcheck "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)
	@printf "$(C_GREEN)✓$(C_RESET) Report: $(BUILDDIR)/linkcheck/output.txt\n"

# ────────────────────────────────────────────────────────────────────
# PDF
# ────────────────────────────────────────────────────────────────────

pdf:
	@printf "$(C_CYAN)→$(C_RESET) Building PDF documentation...\n"
	@$(SPHINXBUILD) -M latexpdf "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)
	@printf "$(C_GREEN)✓$(C_RESET) Built: $(BUILDDIR)/latex/*.pdf\n"

epub:
	@$(SPHINXBUILD) -M epub "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)

# ────────────────────────────────────────────────────────────────────
# Other Sphinx builders (from `sphinx-quickstart`)
# ────────────────────────────────────────────────────────────────────

singlehtml: Makefile
	@$(SPHINXBUILD) -M singlehtml "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)

pickle:
	@$(SPHINXBUILD) -M pickle "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)

json:
	@$(SPHINXBUILD) -M json "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)

htmlhelp:
	@$(SPHINXBUILD) -M htmlhelp "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)

qthelp:
	@$(SPHINXBUILD) -M qthelp "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)

devhelp:
	@$(SPHINXBUILD) -M devhelp "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)

latex:
	@$(SPHINXBUILD) -M latex "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)

latexpdfja:
	@$(SPHINXBUILD) -M latexpdfja "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)

text:
	@$(SPHINXBUILD) -M text "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)

man:
	@$(SPHINXBUILD) -M man "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)

texinfo:
	@$(SPHINXBUILD) -M texinfo "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)

info:
	@$(SPHINXBUILD) -M info "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)

gettext:
	@$(SPHINXBUILD) -M gettext "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)

changes:
	@$(SPHINXBUILD) -M changes "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)

doctest:
	@$(SPHINXBUILD) -M doctest "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)

coverage:
	@$(SPHINXBUILD) -M coverage "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)

dummy:
	@$(SPHINXBUILD) -M dummy "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)

# ────────────────────────────────────────────────────────────────────
# Clean
# ────────────────────────────────────────────────────────────────────

clean:
	@printf "$(C_CYAN)→$(C_RESET) Removing $(BUILDDIR)/\n"
	@rm -rf "$(BUILDDIR)"
	@printf "$(C_GREEN)✓$(C_RESET) Cleaned.\n"

clean-all: clean
	@printf "$(C_CYAN)→$(C_RESET) Removing __pycache__ and .pytest_cache\n"
	@find "$(CURDIR)" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@find "$(CURDIR)" -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	@find "$(CURDIR)" -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	@printf "$(C_GREEN)✓$(C_RESET) All clean.\n"

# ────────────────────────────────────────────────────────────────────
# All (useful in CI)
# ────────────────────────────────────────────────────────────────────

all: strict linkcheck
	@printf "$(C_GREEN)✓$(C_RESET) All builds passed.\n"

# ────────────────────────────────────────────────────────────────────
# Catch-all (from `sphinx-quickstart`)
# ────────────────────────────────────────────────────────────────────

%: Makefile
	@$(SPHINXBUILD) -M $@ "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)

# ────────────────────────────────────────────────────────────────────
# Variables Python
# ────────────────────────────────────────────────────────────────────

PYTHON ?= python3.12
