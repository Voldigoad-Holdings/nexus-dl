.. NexusDL documentation master file.
   You can adapt this file completely to your liking, but it should at
   least contain the root `toctree` directive.

NexusDL
=======

**Multi-site manga, webtoon, comic and doujinshi downloader.**

CLI · TUI · GUI · Web · API

.. image:: https://img.shields.io/badge/python-3.12%2B-blue.svg
   :target: https://www.python.org/
   :alt: Python 3.12+

.. image:: https://img.shields.io/badge/license-GPL--3.0--or--later-green.svg
   :target: https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/LICENSE
   :alt: GPL-3.0-or-later

.. image:: https://github.com/Voldigoad-Holdings/nexus-dl/actions/workflows/test.yml/badge.svg
   :target: https://github.com/Voldigoad-Holdings/nexus-dl/actions/workflows/test.yml
   :alt: CI

.. image:: https://github.com/Voldigoad-Holdings/nexus-dl/actions/workflows/lint.yml/badge.svg
   :target: https://github.com/Voldigoad-Holdings/nexus-dl/actions/workflows/lint.yml
   :alt: Lint

NexusDL is a modern, modular, and extensible downloader for **manga**,
**webtoons**, **comics**, and **doujinshi** from 60+ sites. It is built
around **hexagonal architecture**, **strict typing**, and **async-first**
I/O, and ships with **four interfaces**:

- 🖥️ **CLI** — scriptable, ideal for cron jobs and CI
- 🎛️ **TUI** — interactive terminal UI for power users
- 🪟 **GUI** — desktop app for casual users
- 🌐 **Web** — self-hosted web app with REST/WebSocket API

.. note::

   NexusDL is an **alpha** project (pre-``1.0.0``). Public APIs may change
   until ``1.0.0``. Subscribe to
   `releases <https://github.com/Voldigoad-Holdings/nexus-dl/releases>`_
   for updates.

.. _quickstart:

Quick start
-----------

.. code-block:: bash

   # Install with uv (recommended)
   uv tool install "nexusdl[all]"

   # Configure
   nexusdl config init
   $EDITOR ~/.config/nexusdl/.env

   # Search and download
   nexusdl search "one piece" --sites mangadex
   nexusdl download "https://mangadex.org/title/..." --format CBZ

   # Launch the TUI
   nexusdl-tui

See :doc:`installation/linux`, :doc:`installation/macos`,
:doc:`installation/windows`, or :doc:`installation/docker` for
platform-specific instructions.

.. _documentation:

Documentation
-------------

.. toctree::
   :maxdepth: 2
   :caption: Getting started
   :hidden:

   installation/linux
   installation/macos
   installation/windows
   installation/docker

.. toctree::
   :maxdepth: 2
   :caption: Usage
   :hidden:

   usage/cli
   usage/gui
   usage/web
   usage/configuration
   usage/formats

.. toctree::
   :maxdepth: 2
   :caption: API Reference
   :hidden:

   api/python
   api/rest
   api/websocket

.. toctree::
   :maxdepth: 2
   :caption: Development
   :hidden:

   development/architecture
   development/contributing
   development/adding_sites
   development/adding_parsers
   development/plugins
   development/decisions/index

.. toctree::
   :maxdepth: 1
   :caption: Project
   :hidden:

   GitHub <https://github.com/Voldigoad-Holdings/nexus-dl>
   Changelog <https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/CHANGELOG.md>
   Security <https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/SECURITY.md>
   Code of Conduct <https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/CODE_OF_CONDUCT.md>
   License <https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/LICENSE>

.. _features:

Features
--------

Content
~~~~~~~

- 🗂️ **60+ sites** across 4 languages (FR, EN, KR, and adult)
- 🎯 **Multi-site search** in parallel with unified results
- 📚 **Local library** with **FTS5** full-text search
- 📖 **Reading progress** tracking
- 🔄 **Incremental updates** — only downloads new chapters

Formats
~~~~~~~

- 📁 **FOLDER** — raw image files
- 🗜️ **ZIP** — standard archive
- 📦 **CBZ** — with ``ComicInfo.xml`` metadata
- 📦 **CB7** — 7z variant with metadata
- 📄 **PDF** — one page per image
- 🗃️ **CBT** — tar archive (optional)

.. warning::

   **CBR is not supported** — RAR is proprietary and legally incompatible
   with GPL-3.0. See :doc:`development/decisions/002-no-cbr` for details.

Anti-bot & anti-ban
~~~~~~~~~~~~~~~~~~~

- 🛡️ **Cloudflare bypass** via Playwright and FlareSolverr
- 🔁 **Proxy rotation** with health checks
- ⏱️ **Token-bucket rate limiting** per site
- 🧯 **Circuit breakers** to isolate failing sites
- 💾 **HTTP caching** via ``hishel``
- 🎭 **User-agent rotation**

Developer experience
~~~~~~~~~~~~~~~~~~~~

- 🏗️ **Hexagonal architecture** — ``core/`` never imports ``parsers/``
- 🎯 **Strict typing** — ``mypy --strict`` passes
- 📝 **Google-style docstrings** everywhere
- 🧪 **pytest** + **respx** + **hypothesis**
- 🎨 **ruff** for lint + format
- 🚦 **pre-commit** hooks
- 🐳 **Docker** multi-stage builds
- ⚙️ **GitHub Actions** matrix: Linux + macOS + Windows

Privacy & security
~~~~~~~~~~~~~~~~~~

- 🔐 **All secrets via ``.env``** — never hardcoded
- 🔒 **Encrypted cookies** at rest (``cryptography.Fernet``)
- 🎫 **JWT authentication** for the API
- 🤖 **robots.txt enforcement** (opt-out available)
- 🧱 **Content filtering** opt-in (``NEXUSDL_ALLOW_ADULT=false`` by default)
- 🧨 **``check_imports.py``** fails CI on hardcoded tokens or private keys

.. _interfaces:

Interfaces
----------

.. grid:: 2

   .. grid-item-card:: CLI
      :link: usage/cli
      :link-type: doc

      Scriptable, ideal for cron jobs and CI.

      ``nexusdl``

   .. grid-item-card:: TUI
      :link: usage/cli
      :link-type: doc

      Interactive terminal UI for power users.

      ``nexusdl-tui``

   .. grid-item-card:: GUI
      :link: usage/gui
      :link-type: doc

      Desktop app for casual users.

      ``nexusdl-gui``

   .. grid-item-card:: Web
      :link: usage/web
      :link-type: doc

      Self-hosted web app with REST/WebSocket API.

      ``nexusdl-web``

.. _community:

Community
---------

- 💬 **Discussions** — `GitHub Discussions <https://github.com/Voldigoad-Holdings/nexus-dl/discussions>`_
- 🐛 **Bug reports** — `GitHub Issues <https://github.com/Voldigoad-Holdings/nexus-dl/issues>`_
- 🔒 **Security** — :doc:`SECURITY.md <https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/SECURITY.md>` (see `Security Policy <https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/SECURITY.md>`_)
- 🤝 **Contributing** — :doc:`development/contributing`
- 📜 **Code of Conduct** — `CODE_OF_CONDUCT.md <https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/CODE_OF_CONDUCT.md>`_

.. _license:

License
-------

NexusDL is licensed under the **GNU General Public License v3.0 or later**
(GPL-3.0-or-later).

.. code-block:: text

   NexusDL
   Copyright (C) 2026 Voldigoad-Holdings

   This program is free software: you can redistribute it and/or modify
   it under the terms of the GNU General Public License as published by
   the Free Software Foundation, either version 3 of the License, or
   (at your option) any later version.

   This program is distributed in the hope that it will be useful,
   but WITHOUT ANY WARRANTY; without even the implied warranty of
   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
   GNU General Public License for more details.

See the `full license <https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/LICENSE>`_
for details.

.. _legal:

Legal notice
------------

NexusDL is a **technical tool**. It does **not** host, distribute, or
endorse any content. It downloads content from third-party websites at the
request of the user.

- Users are **solely responsible** for the content they download and for
  complying with the laws of their jurisdiction.
- NexusDL respects ``robots.txt`` and site Terms of Service **by default**
  (``NEXUSDL_RESPECT_ROBOTS_TXT=true``).
- Adult content support (``NEXUSDL_ALLOW_ADULT=true``) is an **opt-in**
  feature for adult users. It is disabled by default.
- **Use at your own risk.**

.. _indices:

Indices and tables
------------------

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
