# ════════════════════════════════════════════════════════════════════
# NexusDL — justfile

# ════════════════════════════════════════════════════════════════════

# ────────────────────────────────────────────────────────────────────
# Configuration globale
# ────────────────────────────────────────────────────────────────────

# Utiliser bash strict mode par défaut (Linux/macOS)
# Sur Windows, `just` utilise `sh` (via Git Bash) ou `cmd.exe` selon dispo.
set shell := ["bash", "-euo", "pipefail", "-c"]

# Échouer si une variable non définie est utilisée
set dotenv-load := true

# Ne pas imprimer chaque commande par défaut (plus propre)
set quiet := false

# Exporter les variables d'environnement (utile pour les recettes Python)
export NEXUSDL_PROJECT_ROOT := justfile_directory()

# ────────────────────────────────────────────────────────────────────
# Variables
# ────────────────────────────────────────────────────────────────────

# Répertoires
src_dir       := "src"
test_dir      := "tests"
docs_dir      := "docs"
frontend_dir  := "src/nexusdl/interfaces/web/frontend"
dist_dir      := "dist"
build_dir     := "build"

# Python
python        := env("PYTHON", "python3.12")
pytest        := python + " -m pytest"
mypy          := python + " -m mypy"
ruff          := python + " -m ruff"

# uv (optionnel)
uv            := env("UV", "uv")

# Node / pnpm
pnpm          := env("PNPM", "pnpm")

# Docker
docker        := env("DOCKER", "docker")
compose       := docker + " compose"

# Image
image_name    := "nexusdl"
image_tag     := env("IMAGE_TAG", "latest")
registry      := "ghcr.io/voldigoad-holdings"

# Plateforme (pour ouvrir un fichier HTML dans le navigateur)
os_name       := os()
open_cmd      := if os_name == "macos" { "open" } else if os_name == "windows" { "start" } else { "xdg-open" }

# Couleurs (désactivées si NO_COLOR est défini)
if env("NO_COLOR", "") == "" {
    color_reset := "\033[0m"
    color_bold  := "\033[1m"
    color_dim   := "\033[2m"
    color_red   := "\033[31m"
    color_green := "\033[32m"
    color_yell  := "\033[33m"
    color_blue  := "\033[34m"
    color_cyan  := "\033[36m"
} else {
    color_reset := ""
    color_bold  := ""
    color_dim   := ""
    color_red   := ""
    color_green := ""
    color_yell  := ""
    color_blue  := ""
    color_cyan  := ""
}

# ────────────────────────────────────────────────────────────────────
# Recette par défaut : liste toutes les recettes
# ────────────────────────────────────────────────────────────────────

# Affiche l'aide (recette par défaut)
default:
    @just --list --list-heading="{{color_bold}}NexusDL — justfile{{color_reset}}\n\n{{color_bold}}Usage:{{color_reset}}\n  just {{color_cyan}}<recipe>{{color_reset}}\n\n{{color_bold}}Recipes:{{color_reset}}\n"

# ────────────────────────────────────────────────────────────────────
# Installation
# ────────────────────────────────────────────────────────────────────

# Installe le projet + extras dev (utilise uv si dispo)
install:
    #!/usr/bin/env bash
    set -euo pipefail
    if command -v {{uv}} >/dev/null 2>&1; then
        echo -e "{{color_cyan}}→{{color_reset}} Installation via uv..."
        {{uv}} python install 3.12
        {{uv}} venv --allow-existing
        {{uv}} pip install -e ".[dev,cli]"
    else
        echo -e "{{color_cyan}}→{{color_reset}} Installation via pip..."
        {{python}} -m venv .venv
        # shellcheck disable=SC1091
        . .venv/bin/activate
        python -m pip install --upgrade pip
        python -m pip install -e ".[dev,cli]"
    fi
    echo -e "{{color_green}}✓{{color_reset}} Installation terminée."
    echo "  Activer le venv : source .venv/bin/activate"
    echo "  Copier .env     : cp .env.example .env && chmod 600 .env"

# Alias de `install`
install-dev: install

# Installe tous les extras (cli, gui, web, playwright, redis, dev)
install-all:
    #!/usr/bin/env bash
    set -euo pipefail
    if command -v {{uv}} >/dev/null 2>&1; then
        {{uv}} pip install -e ".[all]"
    else
        {{python}} -m pip install -e ".[all]"
    fi
    echo -e "{{color_green}}✓{{color_reset}} Tous les extras installés."

# Installe uniquement le CLI + dev
install-cli:
    #!/usr/bin/env bash
    set -euo pipefail
    if command -v {{uv}} >/dev/null 2>&1; then
        {{uv}} pip install -e ".[cli,dev]"
    else
        {{python}} -m pip install -e ".[cli,dev]"
    fi

# Installe uniquement le GUI + dev
install-gui:
    #!/usr/bin/env bash
    set -euo pipefail
    if command -v {{uv}} >/dev/null 2>&1; then
        {{uv}} pip install -e ".[gui,dev]"
    else
        {{python}} -m pip install -e ".[gui,dev]"
    fi

# Installe uniquement le web + dev
install-web:
    #!/usr/bin/env bash
    set -euo pipefail
    if command -v {{uv}} >/dev/null 2>&1; then
        {{uv}} pip install -e ".[web,dev]"
    else
        {{python}} -m pip install -e ".[web,dev]"
    fi

# Met à jour uv.lock
lock:
    #!/usr/bin/env bash
    set -euo pipefail
    if ! command -v {{uv}} >/dev/null 2>&1; then
        echo -e "{{color_red}}✗{{color_reset}} uv non installé." >&2
        exit 1
    fi
    {{uv}} lock
    echo -e "{{color_green}}✓{{color_reset}} uv.lock mis à jour."

# Met à jour toutes les dépendances
upgrade:
    #!/usr/bin/env bash
    set -euo pipefail
    if command -v {{uv}} >/dev/null 2>&1; then
        {{uv}} lock --upgrade
        {{uv}} sync --all-extras
    else
        {{python}} -m pip install --upgrade -e ".[dev,cli]"
    fi
    echo -e "{{color_green}}✓{{color_reset}} Dépendances mises à jour."

# ────────────────────────────────────────────────────────────────────
# Lint & format
# ────────────────────────────────────────────────────────────────────

# Vérifie le code (ruff + mypy + check_imports)
lint:
    echo -e "{{color_cyan}}→{{color_reset}} ruff check..."
    {{ruff}} check {{src_dir}} {{test_dir}}
    echo -e "{{color_cyan}}→{{color_reset}} ruff format --check..."
    {{ruff}} format --check {{src_dir}} {{test_dir}}
    echo -e "{{color_cyan}}→{{color_reset}} mypy..."
    {{mypy}} {{src_dir}}
    echo -e "{{color_cyan}}→{{color_reset}} check_imports (architecture)..."
    {{python}} scripts/check_imports.py
    echo -e "{{color_green}}✓{{color_reset}} Lint OK."

# Corrige automatiquement les problèmes ruff
lint-fix:
    {{ruff}} check {{src_dir}} {{test_dir}} --fix
    {{ruff}} format {{src_dir}} {{test_dir}}
    echo -e "{{color_green}}✓{{color_reset}} Corrections appliquées."

# Formate le code (ruff format + ruff fix)
format:
    {{ruff}} format {{src_dir}} {{test_dir}}
    {{ruff}} check {{src_dir}} {{test_dir}} --fix
    echo -e "{{color_green}}✓{{color_reset}} Formatage terminé."

# Vérifie le formatage sans modifier
format-check:
    {{ruff}} format --check {{src_dir}} {{test_dir}}

# Type checking strict (mypy)
typecheck:
    {{mypy}} {{src_dir}}
    echo -e "{{color_green}}✓{{color_reset}} Typage OK."

# Vérifie les règles d'architecture hexagonale
check-imports:
    {{python}} scripts/check_imports.py

# Valide sites.yaml contre sites_schema.json
validate-sites:
    {{python}} scripts/validate_sites.py

# ────────────────────────────────────────────────────────────────────
# Tests
# ────────────────────────────────────────────────────────────────────

# Lance les tests unitaires (rapides)
test: test-unit

# Lance les tests unitaires
test-unit:
    {{pytest}} {{test_dir}}/unit -q

# Lance les tests d'intégration
test-integration:
    {{pytest}} {{test_dir}}/integration -q -m integration

# Lance les smoke tests (réseau requis)
test-smoke:
    echo -e "{{color_yell}}!{{color_reset}} Tests réseau — peuvent être lents."
    {{pytest}} -q -m smoke

# Tests avec rapport de couverture HTML
test-cov:
    {{pytest}} {{test_dir}}/unit \
        --cov=nexusdl \
        --cov-report=term-missing \
        --cov-report=html:htmlcov
    echo -e "{{color_green}}✓{{color_reset}} Rapport : htmlcov/index.html"
    -{{open_cmd}} htmlcov/index.html 2>/dev/null || true

# Relance les tests à chaque modification
test-watch:
    {{python}} -m pytest_watch {{test_dir}}/unit -- -q

# Lance un fichier de test spécifique
test-file file:
    {{pytest}} {{file}} -v

# Lance un test par nom
test-name pattern:
    {{pytest}} {{test_dir}} -k "{{pattern}}" -v

# ────────────────────────────────────────────────────────────────────
# Vérification complète
# ────────────────────────────────────────────────────────────────────

# Vérifie lint + import rules + tests unitaires (rapide, avant commit)
check: check-fast

# Vérifie lint + import rules + tests unitaires
check-fast:
    echo -e "{{color_bold}}→ Vérification rapide{{color_reset}}"
    just lint
    just validate-sites
    just test-unit
    echo -e "\n{{color_green}}✓{{color_reset}} Prêt à commit."

# Vérifie tout (lint + tests + integration + build)
check-full:
    echo -e "{{color_bold}}→ Vérification complète{{color_reset}}"
    just lint
    just validate-sites
    just test-unit
    just test-integration
    just build
    echo -e "\n{{color_green}}✓{{color_reset}} Tout est OK."

# Lance pre-commit sur les fichiers modifiés
pre-commit:
    pre-commit run --hook-stage pre-commit

# Lance pre-commit sur tous les fichiers
pre-commit-all:
    pre-commit run --all-files

# ────────────────────────────────────────────────────────────────────
# Build
# ────────────────────────────────────────────────────────────────────

# Construit sdist + wheel dans dist/
build: clean-build
    echo -e "{{color_cyan}}→{{color_reset}} Construction du sdist + wheel..."
    #!/usr/bin/env bash
    set -euo pipefail
    if command -v {{uv}} >/dev/null 2>&1; then
        {{uv}} build
    else
        {{python}} -m build
    fi
    echo -e "{{color_green}}✓{{color_reset}} Artefacts dans {{dist_dir}}/"
    ls -lh {{dist_dir}}/

# Construit uniquement le sdist
build-sdist: clean-build
    #!/usr/bin/env bash
    set -euo pipefail
    if command -v {{uv}} >/dev/null 2>&1; then
        {{uv}} build --sdist
    else
        {{python}} -m build --sdist
    fi

# Construit uniquement le wheel
build-wheel: clean-build
    #!/usr/bin/env bash
    set -euo pipefail
    if command -v {{uv}} >/dev/null 2>&1; then
        {{uv}} build --wheel
    else
        {{python}} -m build --wheel
    fi

# ────────────────────────────────────────────────────────────────────
# Nettoyage
# ────────────────────────────────────────────────────────────────────

# Nettoie tous les artefacts et caches
clean: clean-pyc clean-test clean-build clean-frontend

# Supprime les fichiers Python générés
clean-pyc:
    #!/usr/bin/env bash
    find . -type f -name '*.py[cod]' -delete 2>/dev/null || true
    find . -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
    find . -type d -name '*.egg-info' -exec rm -rf {} + 2>/dev/null || true

# Supprime les artefacts de test et de couverture
clean-test:
    #!/usr/bin/env bash
    rm -rf .pytest_cache .mypy_cache .ruff_cache .hypothesis .tox .nox
    rm -rf htmlcov .coverage .coverage.* coverage.xml
    rm -rf tests/.coverage tests/fixtures/tmp

# Supprime les artefacts de build
clean-build:
    rm -rf {{build_dir}} {{dist_dir}} *.egg-info

# Supprime les artefacts frontend
clean-frontend:
    rm -rf {{frontend_dir}}/node_modules
    rm -rf {{frontend_dir}}/.next
    rm -rf {{frontend_dir}}/out
    rm -rf {{frontend_dir}}/.turbo
    rm -rf {{frontend_dir}}/*.tsbuildinfo

# Nettoie tout, y compris le venv
clean-all: clean
    rm -rf .venv venv
    echo -e "{{color_yell}}!{{color_reset}} Nettoyage complet terminé."

# ────────────────────────────────────────────────────────────────────
# Docker
# ────────────────────────────────────────────────────────────────────

# Construit l'image Docker de base
docker-build:
    {{docker}} build -f docker/Dockerfile -t {{image_name}}:{{image_tag}} .

# Construit l'image Docker web
docker-build-web:
    {{docker}} build -f docker/Dockerfile.web -t {{image_name}}-web:{{image_tag}} .

# Construit l'image Docker GUI
docker-build-gui:
    {{docker}} build -f docker/Dockerfile.gui -t {{image_name}}-gui:{{image_tag}} .

# Lance la stack complète
docker-up:
    {{compose}} -f docker/docker-compose.yml up -d

# Arrête la stack
docker-down:
    {{compose}} -f docker/docker-compose.yml down

# Affiche les logs
docker-logs:
    {{compose}} -f docker/docker-compose.yml logs -f

# Pousse les images vers le registry
docker-push:
    {{docker}} tag {{image_name}}:{{image_tag}} {{registry}}/{{image_name}}:{{image_tag}}
    {{docker}} push {{registry}}/{{image_name}}:{{image_tag}}

# ────────────────────────────────────────────────────────────────────
# Documentation
# ────────────────────────────────────────────────────────────────────

# Construit la documentation Sphinx
docs:
    echo -e "{{color_cyan}}→{{color_reset}} Construction de la doc..."
    {{python}} -m sphinx -b html {{docs_dir}}/source {{docs_dir}}/_build/html
    echo -e "{{color_green}}✓{{color_reset}} Doc : {{docs_dir}}/_build/html/index.html"

# Construit et sert la doc sur localhost:8001
docs-serve: docs
    {{python}} -m http.server 8001 --directory {{docs_dir}}/_build/html

# Supprime la doc générée
docs-clean:
    rm -rf {{docs_dir}}/_build

# ────────────────────────────────────────────────────────────────────
# Frontend (Next.js)
# ────────────────────────────────────────────────────────────────────

# Installe les dépendances frontend
frontend-install:
    #!/usr/bin/env bash
    set -euo pipefail
    if ! command -v {{pnpm}} >/dev/null 2>&1; then
        echo -e "{{color_red}}✗{{color_reset}} pnpm non installé. Voir https://pnpm.io/installation" >&2
        exit 1
    fi
    cd {{frontend_dir}} && {{pnpm}} install

# Lance le serveur de dev Next.js (localhost:3000)
frontend-dev:
    cd {{frontend_dir}} && {{pnpm}} dev

# Construit le frontend Next.js (prod)
frontend-build:
    cd {{frontend_dir}} && {{pnpm}} build

# Lint le frontend
frontend-lint:
    cd {{frontend_dir}} && {{pnpm}} lint

# Teste le frontend
frontend-test:
    cd {{frontend_dir}} && {{pnpm}} test

# ────────────────────────────────────────────────────────────────────
# Utilitaires
# ────────────────────────────────────────────────────────────────────

# Lance les benchmarks
benchmark:
    {{python}} scripts/benchmark.py

# Affiche la version du projet
version:
    {{python}} -c "from nexusdl.version import __version__; print(__version__)"

# Affiche la configuration active (secrets masqués par SecretStr)
show-config:
    {{python}} -c "from nexusdl.core.config import load_settings; print(load_settings().model_dump_json(indent=2))"

# Ouvre le repo dans le navigateur
open-repo:
    -{{open_cmd}} "https://github.com/Voldigoad-Holdings/nexus-dl" 2>/dev/null || true

# Ouvre la doc en ligne
open-docs:
    -{{open_cmd}} "https://github.com/Voldigoad-Holdings/nexus-dl/tree/main/docs" 2>/dev/null || true

# Affiche le statut du repo git
status:
    git status --short --branch

# Met à jour depuis le remote principal
update:
    git fetch --all --prune
    git pull --ff-only
