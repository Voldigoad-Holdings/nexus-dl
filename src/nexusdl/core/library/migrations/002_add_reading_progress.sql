-- =============================================================================
-- Migration 002 — reading progress
-- =============================================================================
-- Applied once, in order, after ``001_initial.sql``, by the same
-- forward-only runner in ``nexusdl.core.library.database.LibraryDatabase``.
-- Wrapped in a single transaction like every migration; a failure anywhere
-- rolls the whole file back.
--
-- Purpose
-- -------
-- Persist "the user is currently reading chapter X, page Y of manga Z" so
-- that opening a manga in any interface (CLI, TUI, GUI, Web) resumes at the
-- last position. One record per manga — the progress is a *current
-- position*, not a history. Chapter-level completion is derived from
-- ``page_index == page_count - 1`` at query time and is deliberately not
-- stored.
--
-- Relationship to ``mangas.last_read_at`` (from migration 001)
-- ------------------------------------------------------------
-- ``mangas.last_read_at`` is the **denormalised sort key** used by
-- ``LibraryDatabase.list_mangas()`` to order the library by "recently
-- read" without a join. It is set to the current time on every progress
-- update. It exists as a separate column so the library query does not
-- need to join ``reading_progress`` — a common hot path.
--
-- ``reading_progress.updated_at`` is the **authoritative timestamp** of
-- the last progress save. In v1 the two are always equal; the split
-- exists because future features ("recently active reads" distinct from
-- "recently opened manga") may diverge them, and merging them later would
-- require a breaking migration.
--
-- The two are kept in sync **atomically** by the ``LibraryDatabase``
-- implementation of ``set_reading_progress()``, not by a trigger. Triggers
-- on the hot path are hard to reason about across ORM boundaries and this
-- invariant is already encapsulated in one method.
--
-- Design notes
-- ------------
-- * **Composite foreign key, not two FKs.** ``reading_progress`` does not
--   have a direct FK to ``mangas(id)``. Instead, its composite FK to
--   ``chapters(manga_id, id)`` transitively guarantees that
--   ``chapter.manga_id == reading_progress.manga_id``. This closes a gap
--   that two independent FKs would leave open: with two separate FKs, a
--   caller could store a ``chapter_id`` belonging to a *different* manga
--   without the database noticing.
--
-- * **Chained cascade.** Deleting a manga cascades to its chapters (from
--   migration 001), and deleting a chapter cascades to its reading
--   progress (from this migration). The progress record is therefore
--   always reachable from exactly one live ancestor chain and can never
--   orphan.
--
-- * **The supporting unique index is mandatory.** SQLite requires the
--   parent side of a composite foreign key to be backed by a PRIMARY KEY
--   or UNIQUE constraint. ``chapters`` has ``PRIMARY KEY (id)`` and
--   ``UNIQUE (manga_id, source_id)`` but not ``UNIQUE (manga_id, id)``,
--   so we add a unique index. Because ``id`` is globally unique already,
--   the index is a no-op for data integrity — it exists purely to make
--   the composite FK legal.
--
-- * **``page_index`` is 0-based** to match ``Page.index`` from
--   ``core.models.page``. The database stores the *current* page — the
--   one the user has reached, not the one they have just finished.
--   Progress readers set ``page_index = 0`` when opening a chapter.
--
-- * **Upsert semantics live in the application layer.** The
--   ``PRIMARY KEY (manga_id)`` makes ``INSERT … ON CONFLICT(manga_id) DO
--   UPDATE`` the natural shape for ``set_reading_progress``, but this
--   migration does not encode the upsert — that is the implementation's
--   concern, and encoding it as a trigger here would duplicate the
--   contract the ``LibraryDatabase`` method already enforces.
--
-- * **Chapter validation is at the FK level.** ``set_reading_progress``
--   cannot store a ``chapter_id`` for a chapter that does not exist —
--   the foreign key rejects it, and ``LibraryDatabase`` translates the
--   resulting ``IntegrityError`` into a ``LibraryError`` with the same
--   message the rest of the library API uses.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- Supporting unique index for the composite foreign key
-- -----------------------------------------------------------------------------
-- Required by SQLite so that ``reading_progress`` can reference
-- ``chapters(manga_id, id)`` as a parent key. As explained in the file
-- header, ``(manga_id, id)`` is trivially unique because ``id`` is the
-- primary key of ``chapters`` — the index is purely to satisfy the FK
-- machinery.
-- -----------------------------------------------------------------------------
CREATE UNIQUE INDEX idx_chapters_manga_id_id ON chapters(manga_id, id);


-- -----------------------------------------------------------------------------
-- reading_progress
-- -----------------------------------------------------------------------------
-- One row per manga. Stores the user's current reading position. The
-- primary key on ``manga_id`` makes the table naturally idempotent: the
-- ``LibraryDatabase.set_reading_progress()`` implementation upserts into
-- it, and a re-read of the same manga updates the same row rather than
-- appending history. ``get_reading_progress()`` is a single-row lookup
-- by primary key.
--
-- The composite foreign key is the whole point of the supporting index
-- above: it guarantees that the ``chapter_id`` stored here belongs to
-- ``manga_id``, not to some unrelated manga.
--
-- The ``chapter_id`` index is *not* required by the foreign key — the
-- primary-key lookup on ``manga_id`` makes cascade scans O(1) — but is
-- added for future queries of the form "which mangas have progress in
-- this chapter" (e.g. a future "resume from this chapter across all
-- mangas" feature, or a chapter-level sync endpoint).
-- -----------------------------------------------------------------------------
CREATE TABLE reading_progress (
    manga_id     TEXT    NOT NULL,
    chapter_id   TEXT    NOT NULL,
    page_index   INTEGER NOT NULL DEFAULT 0
                 CHECK (page_index >= 0),
    updated_at   TEXT    NOT NULL,
    PRIMARY KEY (manga_id),
    FOREIGN KEY (manga_id, chapter_id)
        REFERENCES chapters(manga_id, id)
        ON DELETE CASCADE
);

CREATE INDEX idx_reading_progress_chapter_id
    ON reading_progress(chapter_id);
