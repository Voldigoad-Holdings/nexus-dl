
<!--
  Merci de contribuer à NexusDL.
  Ce template est là pour garantir la cohérence du projet — pas pour te
  ralentir. Coche ce qui s'applique, supprime ce qui ne s'applique pas,
  et remplis ce qui est pertinent. Un template partiellement rempli qui
  contient l'essentiel est meilleur qu'un template tout coché sans
  réflexion.
-->

## 📋 Type de changement

<!-- Coche **une seule** catégorie principale. Les changements mixtes
     doivent être découpés en plusieurs PRs quand c'est possible. -->

- [ ] 🐛 **Bug fix** — correction d'un comportement incorrect (non-breaking)
- [ ] ✨ **Nouvelle fonctionnalité** — nouveau comportement (non-breaking)
- [ ] 💥 **Breaking change** — modifie une API publique ou un contrat
- [ ] 📝 **Documentation** — README, docs/source, docstrings uniquement
- [ ] 🏗️ **Refactor** — pas de changement de comportement observable
- [ ] ⚡ **Performance** — amélioration mesurable (benchmark à l'appui)
- [ ] 🔒 **Sécurité** — patch de vulnérabilité (voir `SECURITY.md`)
- [ ] 🔧 **CI / outillage** — workflows, pre-commit, scripts internes
- [ ] 🧪 **Tests** — ajout ou correction de tests (aucun code prod modifié)
- [ ] 🆕 **Nouveau site / parser** — ajout à `data/sites.yaml` + parser
- [ ] 🔌 **Nouveau plugin** — ajout à `plugins/` ou mise à jour de l'API plugin

---

## 📖 Résumé

<!-- 2-4 phrases. Qu'est-ce qui change, et pourquoi ? Pas de "fix bug" —
     explique le comportement AVANT et APRÈS. -->

**Avant :** …

**Après :** …

**Motivation :** <!-- Bug report, ADR, discussion, feature request… -->

Closes #<!-- numéro d'issue -->
Refs <!-- autres issues, ADRs, PRs liées -->

---

## 🏛️ Conformité architecture

<!-- Coche les règles de dépendance qui s'appliquent à cette PR.
     Voir `docs/source/development/architecture.md` et les ADRs. -->

**Règles de dépendance (R1-R6) :**

- [ ] **R1** — `core/` n'importe **pas** `parsers/`, `interfaces/`, `plugins/`
- [ ] **R2** — `parsers/` n'importe **pas** `interfaces/`, `plugins/`
- [ ] **R3** — `plugins/` n'importe **pas** `interfaces/`, `parsers/`
- [ ] **R4** — `interfaces/` peut tout importer (composition root uniquement)
- [ ] **R5** — `data/` n'importe rien de Python (sauf `__init__.py`)
- [ ] **R6** — Communication cross-layer via `Protocols` dans `core/`

**Si cette PR touche une décision architecturale :**

- [ ] Aucun ADR impacté
- [ ] ADR concerné : ADR-00_ — <!-- lien vers docs/source/development/decisions/ -->
- [ ] Nouvel ADR créé : `docs/source/development/decisions/00N-<titre>.md`
      (obligatoire pour tout changement d'architecture, de pattern ou de
      contrat inter-layer)

**Si cette PR modifie le contrat d'une interface publique :**

- [ ] Aucun contrat modifié
- [ ] Contrat modifié + changelog mis à jour + version bump planifié
- [ ] Contrat modifié + breaking change documenté ci-dessous

---

## 🧪 Tests

<!-- Décris ce que tu as testé et comment. -->

**Tests ajoutés :**

- [ ] Tests unitaires (`tests/unit/`) — chemin : `tests/unit/...`
- [ ] Tests d'intégration (`tests/integration/`) — chemin : `tests/integration/...`
- [ ] Fixtures / données de test ajoutées (`tests/fixtures/`)
- [ ] Aucun test ajouté (justification ci-dessous)

**Tests exécutés localement :**

```bash
# Commandes exécutées — remplace par ce que tu as réellement lancé.
pytest tests/unit tests/integration -x -q
mypy --strict src/nexusdl
ruff check src/nexusdl tests
ruff format --check src/nexusdl tests
```

**Résultat :**

- [ ] ✅ Tous les tests passent localement
- [ ] ✅ `mypy --strict` passe sans `# type: ignore` non justifié
- [ ] ✅ `ruff check` + `ruff format --check` passent
- [ ] ✅ `pre-commit run --all-files` passe
- [ ] ⚠️ Anomalies connues : <!-- décris -->

**Couverture** <!-- si pertinent : avant → après -->

```
nexusdl/core/xxx.py   AVANT: 85%   APRÈS: 92%
```

---

## 📚 Documentation

- [ ] Docstrings Google-style ajoutées / mises à jour (fonctions publiques)
- [ ] `docs/source/` mis à jour (Sphinx)
- [ ] `README.md` mis à jour (si l'UX change)
- [ ] `CHANGELOG.md` mis à jour (obligatoire pour toute PR non-doc)
- [ ] `docs/source/api/` mis à jour (si API publique modifiée)
- [ ] Exemple d'usage ajouté (si nouvelle fonctionnalité)
- [ ] Aucune mise à jour nécessaire (justification : …)

**Documentation mise à jour :**

<!-- Liste les fichiers touchés. -->

- `docs/source/...`

---

## 🔒 Sécurité & secrets

<!-- Les règles du projet sont strictes : aucun secret, aucune URL
     d'app en dur, jamais. -->

- [ ] Aucun secret ajouté (API keys, tokens, cookies, passwords)
- [ ] Aucune URL d'application en dur (`nexusdl.dev` ou autre)
- [ ] Aucun nom de site ou de domaine hardcodé hors `data/sites.yaml`
- [ ] Aucun `eval()` / `exec()` / `pickle.loads()` sur des données
      non fiables
- [ ] Aucune dépendance ajoutée sans audit (`pip-audit` si pertinent)
- [ ] Si cette PR corrige une faille : voir `SECURITY.md` —
      ne pas divulguer les détails dans cette PR publique.

**Secrets touchés** (noms uniquement, jamais les valeurs) :

<!-- ex: NEXUSDL_JWT_SECRET (env-var), pas "abc123..." -->

---

## 💥 Breaking changes

<!-- Section obligatoire si tu as coché "Breaking change" ci-dessus. -->

- [ ] Aucun breaking change
- [ ] Breaking change documenté ci-dessous

**Qu'est-ce qui casse ?**

**Migration pour les utilisateurs :**

```python
# Avant
from nexusdl.core import OldSymbol

# Après
from nexusdl.core.new_module import NewSymbol
```

**Version cible du bump :** `X.Y.Z → A.B.C` (SemVer)
**ADR ou CHANGELOG entry :** <!-- lien -->

---

## ✅ Checklist auteur

<!-- Coche tout ce qui s'applique. Cette liste existe pour TOI, pas pour
     le reviewer. Un item non coché = une PR qui va trainer en review. -->

- [ ] La PR est **small and focused** (une seule préoccupation)
- [ ] Le titre de la PR suit la convention : `<type>(<scope>): <description>`
      (ex: `feat(parsers): add mangadex chapters pagination`)
- [ ] Les commits suivent [Conventional Commits](https://www.conventionalcommits.org/)
- [ ] Pas de `TODO` / `FIXME` / `XXX` livrés (sauf justification explicite)
- [ ] Pas de `pass` dans un `except` (jamais — log + re-raise ou gestion)
- [ ] Pas de `print()` — `loguru` avec `.bind()` (voir `core/logger.py`)
- [ ] Pas de `# type: ignore` non justifié
- [ ] `from __future__ import annotations` en tête de chaque fichier Python
- [ ] Aucun code mort, aucun import inutilisé
- [ ] Le code livré est **production-ready** (pas de "je finirai plus tard")

---

## 👀 Checklist reviewer

<!-- C'est au reviewer de cocher ces items avant d'approuver. -->

**Reviewer :** <!-- @ton-nom -->

- [ ] Le diff correspond à la description (pas de scope creep)
- [ ] L'architecture est respectée (R1-R6 vérifiées)
- [ ] Les tests couvrent les cas d'usage réels + les cas d'erreur
- [ ] Les docstrings sont claires et ne paraphrasent pas le code
- [ ] Pas de régression de performance détectable
- [ ] Les messages d'erreur sont utiles (pas juste `Exception`)
- [ ] Pas de nouveau code mort / `except: pass` / `# type: ignore` injustifié
- [ ] La PR est **mergeable** (CI vert, pas de conflit)
- [ ] Le `CHANGELOG.md` est à jour

---

## 🎯 Notes pour le reviewer

<!-- Optionnel. Points d'attention particuliers, décisions douteuses,
     alternatives envisagées, zones où tu aimerais un second avis. -->

---

## 📸 Captures d'écran

<!-- Obligatoire si la PR change l'UI (GUI, TUI, Web frontend).
     Supprimer cette section sinon. -->

**Avant :**

**Après :**

---

<!--
  Rappel : la PR sera fermée si elle ne respecte pas les règles du
  projet (voir CONTRIBUTING.md, CODE_OF_CONDUCT.md, PROMPT MASTER).
  Ce n'est pas personnel — c'est la discipline qui permet à un projet
  de 60+ sites, 4 interfaces et 8 sous-systèmes de rester maintenable.
-->
```

---

## 🎯 Décisions de design du template

| Décision | Justification |
|---|---|
| **11 types de changement** en haut | Le projet a une surface très large (core / parsers / plugins / interfaces / data) — les cas sont réellement distincts |
| **Section architecture R1-R6 explicite** | C'est la règle la plus facile à violer par inadvertance, et la plus coûteuse à corriger après merge |
| **ADR obligatoire pour changement architectural** | Force à documenter les décisions durables, cohérent avec les 5 ADRs existants |
| **Checklist secrets séparée** | Le projet a des règles très strictes (aucun secret, aucune URL hardcodée) — ça mérite sa propre section pour ne pas être noyée |
| **Section Breaking Change avec exemple de migration** | Force à penser à l'impact utilisateur *avant* de merger |
| **Checklist auteur ≠ checklist reviewer** | Séparation des responsabilités — le reviewer ne doit pas faire le travail de l'auteur |
| **Conventional Commits mentionné** | Cohérent avec `CHANGELOG.md` automatisable |
| **Section "Notes pour le reviewer"** | Encourage les PRs honnêtes (zones d'incertitude déclarées) plutôt que la sur-confiance |
| **Commentaire final "ce n'est pas personnel"** | Culture d'équipe — la discipline n'est pas de la rigidité |
| **Longueur ~140 lignes** | Assez complet pour être utile, assez court pour être lu. Au-delà, les gens le skippent |

---

## ⚠️ Fichiers compagnons recommandés

Ton TREE liste aussi ces templates d'issues — je peux les livrer si tu veux, dans la même veine :

| Fichier | Contenu attendu |
|---|---|
| `.github/ISSUE_TEMPLATE/bug_report.md` | Reproduction minimale, version, OS, logs (redacted), comportement attendu vs observé |
| `.github/ISSUE_TEMPLATE/feature_request.md` | Cas d'usage, alternatives considérées, impact sur l'archi, willingness to implement |
| `.github/ISSUE_TEMPLATE/new_site.md` | Nom du site, URL, langues, adulte ou non, capabilities, exemplaire de HTML/API à scraper |
| `.github/CODEOWNERS` | Règles de review (ex: `*` → toi, `parsers/fr/` → reviewer FR, `docs/` → docs owner) |
| `.github/dependabot.yml` | Groupes de mise à jour (python, npm, docker, github-actions), schedule hebdo |

Dis-moi si tu veux que j'enchaîne sur un de ces fichiers, ou si on bascule sur le vrai travail de fond (`core/exceptions.py` — le premier fichier d'implémentation core).
