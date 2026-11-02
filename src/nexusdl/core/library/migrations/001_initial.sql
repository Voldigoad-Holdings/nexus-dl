-- =============================================================================
-- Migration 001 — initial library schema
-- =============================================================================
-- Applied once, in order, by ``nexusdl.core.library.database.LibraryDatabase``
-- via its ``init()`` method. The runner is forward-only and wraps each file
-- in a single transaction; a failure anywhere in this file rolls the entire
-- migration back, leaving the database at the previous version.
--
-- Version tracking lives in the runner's own ``schema_version`` table, not
-- here. That table is bootstrapped by the runner *before* this migration is
-- applied, so this file must not assume or create it.
--
-- Connection-level PRAGMAs (``foreign_keys = ON``, ``journal_mode = WAL``,
-- ``synchronous = NORMAL``, ``busy_timeout``) are configured by
-- ``database.py`` on every connection and are deliberately absent here —
-- they are not schema features and a migration is the wrong place for them.
--
-- Conventions used throughout
--   * Ids are TEXT. Manga and chapter ids are 16-character lowercase hex
--     (sha256 truncation); user ids are 36-character canonical UUID4 strings.
--     The exact length is enforced by CHECK constraints so a bug in the
--     application layer cannot silently corrupt the schema contract.
--   * Timestamps are ISO 8601 strings with a 'Z' suffix (UTC). Stored as
--     TEXT so SQLite's default collation sorts them chronologically.
--   * Enums are TEXT with CHECK constraints mirroring the Python enum
--     members one-for-one. Renaming a value in ``core.models.enums`` is a
--     breaking change requiring a new migration.
--   * Booleans are INTEGER (0 / 1) with CHECK constraints. SQLite has no
--     native boolean type.
--   * JSON-shaped columns (``alt_titles``, ``tags``) are TEXT holding a
--     JSON array. SQLite's JSON1 extension is used at query time by
--     ``core.library.search``; the columns themselves are plain TEXT so
--     they can be inspected by any client.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- users
-- -----------------------------------------------------------------------------
-- Minimal auth table for the REST API's JWT flow. The CLI, TUI, and GUI
-- backends can run without any user row — this table is empty on a purely
-- local installation.
--
-- ``password_hash`` stores the full self-describing output of the hash
-- function (bcrypt string, includes algorithm prefix and cost); the
-- database never inspects it.
-- -----------------------------------------------------------------------------
CREATE TABLE users (
    id              TEXT    PRIMARY KEY
                    CHECK (length(id) = 36),
    email           TEXT    NOT NULL UNIQUE,
    password_hash   TEXT    NOT NULL,
    role            TEXT    NOT NULL DEFAULT 'USER'
                    CHECK (role IN ('GUEST', 'USER', 'ADMIN')),
    is_active       INTEGER NOT NULL DEFAULT 1
                    CHECK (is_active IN (0, 1)),
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    last_login_at   TEXT
);

CREATE INDEX idx_users_email ON users(email);


-- -----------------------------------------------------------------------------
-- mangas
-- -----------------------------------------------------------------------------
-- One row per manga the library knows about. Populated by a download, a
-- library import, or a manual add. The primary key is deterministic:
--
--     id = sha256(f"{site_id}:{source_id}")[:16]
--
-- so re-adding the same manga from the same site hits the existing row
-- (``add_manga`` performs an upsert). The UNIQUE constraint on
-- ``(site_id, source_id)`` is the guard against a hash collision silently
-- merging two distinct mangas: if two rows ever share both columns, the
-- second insert fails, which is a loud, fixable signal.
-- -----------------------------------------------------------------------------
CREATE TABLE mangas (
    id              TEXT    PRIMARY KEY
                    CHECK (length(id) = 16),
    site_id         TEXT    NOT NULL,
    source_id       TEXT    NOT NULL,
    title           TEXT    NOT NULL,
    alt_titles      TEXT,
    description     TEXT,
    author          TEXT,
    artist          TEXT,
    status          TEXT    NOT NULL DEFAULT 'UNKNOWN'
                    CHECK (status IN (
                        'ONGOING', 'COMPLETED', 'HIATUS',
                        'CANCELLED', 'UNKNOWN'
                    )),
    language        TEXT    NOT NULL DEFAULT 'UNKNOWN'
                    CHECK (language IN (
                        'FR', 'EN', 'JA', 'KO', 'ZH', 'ES',
                        'PT', 'IT', 'DE', 'RU', 'AR', 'UNKNOWN'
                    )),
    content_rating  TEXT    NOT NULL DEFAULT 'SAFE'
                    CHECK (content_rating IN (
                        'SAFE', 'SUGGESTIVE', 'EROTICA', 'PORNOGRAPHIC'
                    )),
    is_manga        INTEGER NOT NULL DEFAULT 1
                    CHECK (is_manga IN (0, 1)),
    year            INTEGER,
    cover_url       TEXT,
    cover_path      TEXT,
    url             TEXT    NOT NULL,
    tags            TEXT,
    page_count      INTEGER NOT NULL DEFAULT 0
                    CHECK (page_count >= 0),
    chapter_count   INTEGER NOT NULL DEFAULT 0
                    CHECK (chapter_count >= 0),
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    last_read_at    TEXT,
    UNIQUE (site_id, source_id)
);

CREATE INDEX idx_mangas_site_id      ON mangas(site_id);
CREATE INDEX idx_mangas_title        ON mangas(title);
CREATE INDEX idx_mangas_language     ON mangas(language);
CREATE INDEX idx_mangas_status       ON mangas(status);
CREATE INDEX idx_mangas_last_read_at ON mangas(last_read_at);


-- -----------------------------------------------------------------------------
-- chapters
-- -----------------------------------------------------------------------------
-- One row per chapter belonging to a manga row. The primary key is
-- deterministic:
--
--     id = sha256(f"{manga_id}:{source_id}")[:16]
--
-- ``number`` is TEXT (not REAL) so that specials, "10.5", and "SP1" are
-- representable without ambiguity. Sorting is done at query time by
-- ``core.library.search`` using a numeric-aware collation function.
--
-- The ON DELETE CASCADE means ``delete_manga()`` cleans up chapters in
-- a single statement. Reading progress (migration 002) cascades from here.
-- -----------------------------------------------------------------------------
CREATE TABLE chapters (
    id              TEXT    PRIMARY KEY
                    CHECK (length(id) = 16),
    manga_id        TEXT    NOT NULL
                    REFERENCES mangas(id) ON DELETE CASCADE,
    source_id       TEXT    NOT NULL,
    title           TEXT    NOT NULL,
    number          TEXT,
    volume          TEXT,
    language        TEXT    NOT NULL DEFAULT 'UNKNOWN'
                    CHECK (language IN (
                        'FR', 'EN', 'JA', 'KO', 'ZH', 'ES',
                        'PT', 'IT', 'DE', 'RU', 'AR', 'UNKNOWN'
                    )),
    page_count      INTEGER NOT NULL DEFAULT 0
                    CHECK (page_count >= 0),
    url             TEXT    NOT NULL,
    published_at    TEXT,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    UNIQUE (manga_id, source_id)
);

CREATE INDEX idx_chapters_manga_id        ON chapters(manga_id);
CREATE INDEX idx_chapters_manga_number    ON chapters(manga_id, number);
CREATE INDEX idx_chapters_published_at    ON chapters(published_at);


-- -----------------------------------------------------------------------------
-- mangas_fts — full-text search over manga metadata
-- -----------------------------------------------------------------------------
-- External-content FTS5 table: the text is stored once, in ``mangas``; FTS5
-- indexes tokens and keeps a reverse map back to ``mangas.rowid``. This is
-- the canonical FTS5 pattern for "search an existing column without
-- duplicating it".
--
-- ``content_rowid='rowid'`` is correct: ``mangas`` is a normal rowid table
-- (TEXT PRIMARY KEY does not imply WITHOUT ROWID). The FTS index keys on
-- the physical ``rowid``, so the joins at query time are:
--
--     SELECT m.* FROM mangas_fts f
--     JOIN mangas m ON m.rowid = f.rowid
--     WHERE mangas_fts MATCH ?
--     ORDER BY bm25(mangas_fts);
--
-- Tokenizer ``unicode61 remove_diacritics 2``:
--   * Lowercases and folds accents (é → e), matching user expectations for
--     Latin titles.
--   * Treats CJK codepoints as individual tokens, matching Mihon and
--     Kavita's behaviour for Japanese, Korean, and Chinese titles. FTS5
--     does not have a built-in CJK tokenizer; ``unicode61`` is the
--     best-available portable choice.
-- -----------------------------------------------------------------------------
CREATE VIRTUAL TABLE mangas_fts USING fts5(
    title,
    alt_titles,
    author,
    description,
    content='mangas',
    content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);


-- -----------------------------------------------------------------------------
-- FTS sync triggers
-- -----------------------------------------------------------------------------
-- The 'delete' command with the FTS5 external-content idiom removes the
-- index entry corresponding to a row without touching the content table.
-- This pattern is mandatory for external-content tables; a plain DELETE
-- on mangas_fts would fail because the table has no writable content of
-- its own.
-- -----------------------------------------------------------------------------

CREATE TRIGGER mangas_ai AFTER INSERT ON mangas BEGIN
    INSERT INTO mangas_fts(rowid, title, alt_titles, author, description)
    VALUES (
        new.rowid,
        new.title,
        COALESCE(new.alt_titles,  ''),
        COALESCE(new.author,      ''),
        COALESCE(new.description, '')
    );
END;

CREATE TRIGGER mangas_ad AFTER DELETE ON mangas BEGIN
    INSERT INTO mangas_fts(
        mangas_fts, rowid, title, alt_titles, author, description
    )
    VALUES (
        'delete',
        old.rowid,
        old.title,
        COALESCE(old.alt_titles,  ''),
        COALESCE(old.author,      ''),
        COALESCE(old.description, '')
    );
END;

CREATE TRIGGER mangas_au AFTER UPDATE ON mangas BEGIN
    INSERT INTO mangas_fts(
        mangas_fts, rowid, title, alt_titles, author, description
    )
    VALUES (
        'delete',
        old.rowid,
        old.title,
        COALESCE(old.alt_titles,  ''),
        COALESCE(old.author,      ''),
        COALESCE(old.description, '')
    );
    INSERT INTO mangas_fts(rowid, title, alt_titles, author, description)
    VALUES (
        new.rowid,
        new.title,
        COALESCE(new.alt_titles,  ''),
        COALESCE(new.author,      ''),
        COALESCE(new.description, '')
    );
END;
