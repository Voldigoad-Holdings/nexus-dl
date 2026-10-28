# ════════════════════════════════════════════════════════════════════
# NexusDL — Makefile
#
# Commandes principales :
#   make install         Installe le projet + extras dev (via uv)
#   make install-all     Installe tous les extras (cli, gui, web, playwright)
#   make lint            Vérifie le code (ruff + mypy + check_imports)
#   make format          Formate le code (ruff format + ruff fix)
#   make test            Lance les tests unitaires
#   make test-all        Lance tous les tests (unit + integration)
#   make test-cov        Tests avec rapport de couverture HTML
#   make check           Vérifie tout avant commit (lint + test + import rules)
#   make build           Construit sdist + wheel dans dist/
#   make clean           Nettoie les artefacts et caches
#   make docker-build    Construit l'image Docker de base
#   make docker-up       Lance la stack complète via docker compose
#   make docs            Construit la documentation Sphinx
#   make frontend-dev    Lance le serveur de dev Next.js
#   make frontend-build  Construit le frontend Next.js
#
# Cross-platform :
#   - Linux / macOS : natif
#   - Windows       : Git Bash, Chocolatey `make`, ou WSL
#
# ⚠️  Aucune commande ne doit nécessiter de secret en dur.
# ⚠️  Aucune commande ne doit modifier des fichiers versionnés.
# ════════════════════════════════════════════════════════════════════

# ────────────────────────────────────────────────────────────────────
# Configuration
# ────────────────────────────────────────────────────────────────────

# Utiliser bash strict mode pour toutes les recettes
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

# Désactiver les commandes implicites et préserver les fichiers intermédiaires
MAKEFLAGS += --no-builtin-rules --no-builtin-variables
.SUFFIXES:

# Répertoires
SRC_DIR       := src
TEST_DIR      := tests
DOCS_DIR      := docs
FRONTEND_DIR  := src/nexusdl/interfaces/web/frontend
DIST_DIR      := dist
BUILD_DIR     := build

# Python
PYTHON        ?= python3.12
PIP           ?= $(PYTHON) -m pip
PYTEST        ?= $(PYTHON) -m pytest
MYPY          ?= $(PYTHON) -m mypy
RUFF          ?= $(PYTHON) -m ruff

# uv (optionnel, utilisé si présent)
UV            ?= uv
HAS_UV        := $(shell command -v $(UV) 2>/dev/null || echo "")

# Node / pnpm (optionnel, pour le frontend)
PNPM          ?= pnpm
HAS_PNPM      := $(shell command -v $(PNPM) 2>/dev/null || echo "")

# Docker
DOCKER        ?= docker
COMPOSE       ?= $(DOCKER) compose

# Image
IMAGE_NAME    := nexusdl
IMAGE_TAG     ?= latest
REGISTRY      ?= ghcr.io/voldigoad-holdings

# Plateforme
OS            := $(shell uname -s 2>/dev/null || echo "Unknown")
ifeq ($(OS),Darwin)
    OPEN_CMD := open
else ifeq ($(OS),Linux)
    OPEN_CMD := xdg-open
else
    OPEN_CMD := start
endif

# Couleurs (désactivées si NO_COLOR est défini)
ifdef NO_COLOR
    C_RESET :=
    C_BOLD  :=
    C_DIM   :=
    C_RED   :=
    C_GREEN :=
    C_YELL  :=
    C_BLUE  :=
    C_CYAN  :=
else
    C_RESET := \033[0m
    C_BOLD  := \033[1m
    C_DIM   := \033[2m
    C_RED   := \033[31m
    C_GREEN := \033[32m
    C_YELL  := \033[33m
    C_BLUE  := \033[34m
    C_CYAN  := \033[36m
endif

# ────────────────────────────────────────────────────────────────────
# Cibles phony (toutes les commandes, aucun fichier réel)
# ────────────────────────────────────────────────────────────────────

.PHONY: help \
        install install-dev install-all install-cli install-gui install-web \
        lock upgrade \
        lint lint-fix format format-check \
        typecheck check-imports validate-sites \
        test test-unit test-integration test-smoke test-cov test-watch \
        check check-fast check-full \
        pre-commit pre-commit-all \
        build build-sdist build-wheel \
        clean clean-pyc clean-test clean-build clean-frontend clean-all \
        docker-build docker-build-web docker-build-gui docker-up docker-down docker-logs docker-push \
        docs docs-clean docs-serve \
        frontend-install frontend-dev frontend-build frontend-lint frontend-test \
        benchmark \
        version show-config

# ────────────────────────────────────────────────────────────────────
# Aide (cible par défaut)
# ────────────────────────────────────────────────────────────────────

.DEFAULT_GOAL := help

help: ## Affiche cette aide
	@printf "$(C_BOLD)NexusDL — Makefile$(C_RESET)\n\n"
	@printf "$(C_BOLD)Usage:$(C_RESET)\n"
	@printf "  make $(C_CYAN)<target>$(C_RESET)\n\n"
	@printf "$(C_BOLD)Cibles disponibles:$(C_RESET)\n"
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z_-]+:.*?##/ { printf "  $(C_CYAN)%-22s$(C_RESET) %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@printf "\n$(C_BOLD)Environnement:$(C_RESET)\n"
	@printf "  Python  : $(PYTHON)\n"
	@printf "  uv      : $(if $(HAS_UV),$(C_GREEN)oui$(C_RESET),$(C_DIM)non (installer: curl -LsSf https://astral.sh/uv/install.sh | sh)$(C_RESET))\n"
	@printf "  pnpm    : $(if $(HAS_PNPM),$(C_GREEN)oui$(C_RESET),$(C_DIM)non (optionnel, pour le frontend)$(C_RESET))\n"
	@printf "  Docker  : $(if $(shell command -v $(DOCKER) 2>/dev/null),$(C_GREEN)oui$(C_RESET),$(C_DIM)non (optionnel)$(C_RESET))\n"

# ────────────────────────────────────────────────────────────────────
# Installation
# ────────────────────────────────────────────────────────────────────

install: ## Installe le projet + extras dev (via uv si dispo)
ifdef HAS_UV
	@printf "$(C_CYAN)→$(C_RESET) Installation via uv...\n"
	$(UV) python install 3.12
	$(UV) venv --allow-existing
	$(UV) pip install -e ".[dev,cli]"
else
	@printf "$(C_CYAN)→$(C_RESET) Installation via pip...\n"
	$(PYTHON) -m venv .venv
	. .venv/bin/activate && $(PIP) install --upgrade pip && $(PIP) install -e ".[dev,cli]"
endif
	@printf "$(C_GREEN)✓$(C_RESET) Installation terminée.\n"
	@printf "  Activer le venv : source .venv/bin/activate\n"
	@printf "  Copier .env     : cp .env.example .env && chmod 600 .env\n"

install-dev: install ## Alias de `install`

install-all: ## Installe tous les extras (cli, gui, web, playwright, redis, dev)
ifdef HAS_UV
	$(UV) pip install -e ".[all]"
else
	$(PIP) install -e ".[all]"
endif
	@printf "$(C_GREEN)✓$(C_RESET) Tous les extras installés.\n"

install-cli: ## Installe uniquement le CLI + dev
ifdef HAS_UV
	$(UV) pip install -e ".[cli,dev]"
else
	$(PIP) install -e ".[cli,dev]"
endif

install-gui: ## Installe uniquement le GUI + dev
ifdef HAS_UV
	$(UV) pip install -e ".[gui,dev]"
else
	$(PIP) install -e ".[gui,dev]"
endif

install-web: ## Installe uniquement le web + dev
ifdef HAS_UV
	$(UV) pip install -e ".[web,dev]"
else
	$(PIP) install -e ".[web,dev]"
endif

lock: ## Met à jour uv.lock (si uv présent)
ifndef HAS_UV
	@printf "$(C_RED)✗$(C_RESET) uv non installé.\n"
	@exit 1
endif
	$(UV) lock
	@printf "$(C_GREEN)✓$(C_RESET) uv.lock mis à jour.\n"

upgrade: ## Met à jour toutes les dépendances
ifdef HAS_UV
	$(UV) lock --upgrade
	$(UV) sync --all-extras
else
	$(PIP) install --upgrade -e ".[dev,cli]"
endif
	@printf "$(C_GREEN)✓$(C_RESET) Dépendances mises à jour.\n"

# ────────────────────────────────────────────────────────────────────
# Lint & format
# ────────────────────────────────────────────────────────────────────

lint: ## Vérifie le code (ruff + mypy + check_imports)
	@printf "$(C_CYAN)→$(C_RESET) ruff check...\n"
	$(RUFF) check $(SRC_DIR) $(TEST_DIR)
	@printf "$(C_CYAN)→$(C_RESET) ruff format --check...\n"
	$(RUFF) format --check $(SRC_DIR) $(TEST_DIR)
	@printf "$(C_CYAN)→$(C_RESET) mypy...\n"
	$(MYPY) $(SRC_DIR)
	@printf "$(C_CYAN)→$(C_RESET) check_imports (architecture)...\n"
	$(PYTHON) scripts/check_imports.py
	@printf "$(C_GREEN)✓$(C_RESET) Lint OK.\n"

lint-fix: ## Corrige automatiquement les problèmes ruff
	$(RUFF) check $(SRC_DIR) $(TEST_DIR) --fix
	$(RUFF) format $(SRC_DIR) $(TEST_DIR)
	@printf "$(C_GREEN)✓$(C_RESET) Corrections appliquées.\n"

format: ## Formate le code (ruff format + ruff fix)
	$(RUFF) format $(SRC_DIR) $(TEST_DIR)
	$(RUFF) check $(SRC_DIR) $(TEST_DIR) --fix
	@printf "$(C_GREEN)✓$(C_RESET) Formatage terminé.\n"

format-check: ## Vérifie le formatage sans modifier
	$(RUFF) format --check $(SRC_DIR) $(TEST_DIR)

typecheck: ## Type checking strict (mypy)
	$(MYPY) $(SRC_DIR)
	@printf "$(C_GREEN)✓$(C_RESET) Typage OK.\n"

check-imports: ## Vérifie les règles d'architecture hexagonale
	$(PYTHON) scripts/check_imports.py

validate-sites: ## Valide sites.yaml contre sites_schema.json
	$(PYTHON) scripts/validate_sites.py

# ────────────────────────────────────────────────────────────────────
# Tests
# ────────────────────────────────────────────────────────────────────

test: test-unit ## Alias de `test-unit`

test-unit: ## Lance les tests unitaires (rapides)
	$(PYTEST) $(TEST_DIR)/unit -q

test-integration: ## Lance les tests d'intégration (peuvent utiliser le disque)
	$(PYTEST) $(TEST_DIR)/integration -q -m integration

test-smoke: ## Lance les smoke tests (réseau requis)
	@printf "$(C_YELL)!$(C_RESET) Tests réseau — peuvent être lents.\n"
	$(PYTEST) -q -m smoke

test-cov: ## Tests avec rapport de couverture HTML
	$(PYTEST) $(TEST_DIR)/unit \
		--cov=nexusdl \
		--cov-report=term-missing \
		--cov-report=html:htmlcov
	@printf "$(C_GREEN)✓$(C_RESET) Rapport : htmlcov/index.html\n"
	@$(OPEN_CMD) htmlcov/index.html 2>/dev/null || true

test-watch: ## Relance les tests à chaque modification (nécessite pytest-watch)
	$(PYTHON) -m pytest_watch $(TEST_DIR)/unit -- -q

# ────────────────────────────────────────────────────────────────────
# Vérification complète (pre-commit local)
# ────────────────────────────────────────────────────────────────────

check: check-fast ## Alias de `check-fast`

check-fast: ## Vérifie lint + import rules (rapide, avant commit)
	@printf "$(C_BOLD)→ Vérification rapide$(C_RESET)\n"
	@$(MAKE) lint
	@$(MAKE) validate-sites
	@$(MAKE) test-unit
	@printf "\n$(C_GREEN)✓$(C_RESET) Prêt à commit.\n"

check-full: ## Vérifie tout (lint + tests + integration + build)
	@printf "$(C_BOLD)→ Vérification complète$(C_RESET)\n"
	@$(MAKE) lint
	@$(MAKE) validate-sites
	@$(MAKE) test-unit
	@$(MAKE) test-integration
	@$(MAKE) build
	@printf "\n$(C_GREEN)✓$(C_RESET) Tout est OK.\n"

pre-commit: ## Lance pre-commit sur les fichiers modifiés
	pre-commit run --hook-stage pre-commit

pre-commit-all: ## Lance pre-commit sur tous les fichiers
	pre-commit run --all-files

# ────────────────────────────────────────────────────────────────────
# Build
# ────────────────────────────────────────────────────────────────────

build: clean-build ## Construit sdist + wheel dans dist/
	@printf "$(C_CYAN)→$(C_RESET) Construction du sdist + wheel...\n"
ifdef HAS_UV
	$(UV) build
else
	$(PYTHON) -m build
endif
	@printf "$(C_GREEN)✓$(C_RESET) Artefacts dans $(DIST_DIR)/\n"
	@ls -lh $(DIST_DIR)/

build-sdist: clean-build ## Construit uniquement le sdist
ifdef HAS_UV
	$(UV) build --sdist
else
	$(PYTHON) -m build --sdist
endif

build-wheel: clean-build ## Construit uniquement le wheel
ifdef HAS_UV
	$(UV) build --wheel
else
	$(PYTHON) -m build --wheel
endif

# ────────────────────────────────────────────────────────────────────
# Nettoyage
# ────────────────────────────────────────────────────────────────────

clean: clean-pyc clean-test clean-build clean-frontend ## Nettoie tous les artefacts et caches

clean-pyc: ## Supprime les fichiers Python générés
	find . -type f -name '*.py[cod]' -delete 2>/dev/null || true
	find . -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name '*.egg-info' -exec rm -rf {} + 2>/dev/null || true

clean-test: ## Supprime les artefacts de test et de couverture
	rm -rf .pytest_cache .mypy_cache .ruff_cache .hypothesis .tox .nox
	rm -rf htmlcov .coverage .coverage.* coverage.xml
	rm -rf tests/.coverage tests/fixtures/tmp

clean-build: ## Supprime les artefacts de build
	rm -rf $(BUILD_DIR) $(DIST_DIR) *.egg-info

clean-frontend: ## Supprime les artefacts frontend (node_modules, .next)
	rm -rf $(FRONTEND_DIR)/node_modules
	rm -rf $(FRONTEND_DIR)/.next
	rm -rf $(FRONTEND_DIR)/out
	rm -rf $(FRONTEND_DIR)/.turbo
	rm -rf $(FRONTEND_DIR)/*.tsbuildinfo

clean-all: clean ## Nettoie tout, y compris le venv
	rm -rf .venv venv
	@printf "$(C_YELL)!$(C_RESET) Nettoyage complet terminé.\n"

# ────────────────────────────────────────────────────────────────────
# Docker
# ────────────────────────────────────────────────────────────────────

docker-build: ## Construit l'image Docker de base
	$(DOCKER) build -f docker/Dockerfile -t $(IMAGE_NAME):$(IMAGE_TAG) .

docker-build-web: ## Construit l'image Docker web (full stack)
	$(DOCKER) build -f docker/Dockerfile.web -t $(IMAGE_NAME)-web:$(IMAGE_TAG) .

docker-build-gui: ## Construit l'image Docker GUI (X11)
	$(DOCKER) build -f docker/Dockerfile.gui -t $(IMAGE_NAME)-gui:$(IMAGE_TAG) .

docker-up: ## Lance la stack complète (docker compose up -d)
	$(COMPOSE) -f docker/docker-compose.yml up -d

docker-down: ## Arrête la stack
	$(COMPOSE) -f docker/docker-compose.yml down

docker-logs: ## Affiche les logs de la stack
	$(COMPOSE) -f docker/docker-compose.yml logs -f

docker-push: ## Pousse les images vers le registry
	$(DOCKER) tag $(IMAGE_NAME):$(IMAGE_TAG) $(REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG)
	$(DOCKER) push $(REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG)

# ────────────────────────────────────────────────────────────────────
# Documentation
# ────────────────────────────────────────────────────────────────────

docs: ## Construit la documentation Sphinx
	@printf "$(C_CYAN)→$(C_RESET) Construction de la doc...\n"
	$(PYTHON) -m sphinx -b html $(DOCS_DIR)/source $(DOCS_DIR)/_build/html
	@printf "$(C_GREEN)✓$(C_RESET) Doc : $(DOCS_DIR)/_build/html/index.html\n"

docs-serve: docs ## Construit et sert la doc sur localhost:8001
	$(PYTHON) -m http.server 8001 --directory $(DOCS_DIR)/_build/html

docs-clean: ## Supprime la doc générée
	rm -rf $(DOCS_DIR)/_build

# ────────────────────────────────────────────────────────────────────
# Frontend (Next.js)
# ────────────────────────────────────────────────────────────────────

frontend-install: ## Installe les dépendances frontend
ifndef HAS_PNPM
	@printf "$(C_RED)✗$(C_RESET) pnpm non installé. Voir https://pnpm.io/installation\n"
	@exit 1
endif
	cd $(FRONTEND_DIR) && $(PNPM) install

frontend-dev: ## Lance le serveur de dev Next.js (localhost:3000)
	cd $(FRONTEND_DIR) && $(PNPM) dev

frontend-build: ## Construit le frontend Next.js (prod)
	cd $(FRONTEND_DIR) && $(PNPM) build

frontend-lint: ## Lint le frontend
	cd $(FRONTEND_DIR) && $(PNPM) lint

frontend-test: ## Teste le frontend
	cd $(FRONTEND_DIR) && $(PNPM) test

# ────────────────────────────────────────────────────────────────────
# Utilitaires
# ────────────────────────────────────────────────────────────────────

benchmark: ## Lance les benchmarks (scripts/benchmark.py)
	$(PYTHON) scripts/benchmark.py

version: ## Affiche la version du projet
	@$(PYTHON) -c "from nexusdl.version import __version__; print(__version__)"

show-config: ## Affiche la configuration active (secrets masqués)
	@$(PYTHON) -c "from nexusdl.core.config import settings; print(settings.model_dump_json(indent=2))"
