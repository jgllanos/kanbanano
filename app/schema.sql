-- Idempotent: run on every startup.
--
-- Timestamps use millisecond precision because cards.updated_at is the
-- optimistic-concurrency token for description edits; second resolution would
-- let two saves in the same second both pass the check.
--
-- position is REAL so a later switch to fractional indexing needs no migration.
-- Today it always holds contiguous integers 0, 1, 2… Always ORDER BY position, id.

CREATE TABLE IF NOT EXISTS boards (
    id          INTEGER PRIMARY KEY,
    title       TEXT    NOT NULL,
    background  TEXT    NOT NULL DEFAULT '#0079bf',  -- '#rrggbb' or 'image:<token>', see board_images
    version     INTEGER NOT NULL DEFAULT 0,          -- bumped on every mutation, see db.bump_version
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now'))
);

-- An uploaded board background, at most one per board. The bytes are here
-- instead of in boards.background because that row is read on every poll and
-- every write, and a photo would be read along with it each time. The token is
-- part of the image's URL, so a new upload is served from a URL nothing has
-- cached.
CREATE TABLE IF NOT EXISTS board_images (
    board_id      INTEGER PRIMARY KEY REFERENCES boards(id) ON DELETE CASCADE,
    token         TEXT    NOT NULL,
    content_type  TEXT    NOT NULL,
    data          BLOB    NOT NULL
);

CREATE TABLE IF NOT EXISTS lists (
    id        INTEGER PRIMARY KEY,
    board_id  INTEGER NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
    title     TEXT    NOT NULL,
    position  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS cards (
    id           INTEGER PRIMARY KEY,
    list_id      INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    title        TEXT    NOT NULL,
    description  TEXT    NOT NULL DEFAULT '',  -- raw text; rendered as Markdown later
    position     REAL    NOT NULL,
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now')),
    updated_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now'))
);

CREATE TABLE IF NOT EXISTS labels (
    id        INTEGER PRIMARY KEY,
    board_id  INTEGER NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
    name      TEXT    NOT NULL,
    color     TEXT    NOT NULL DEFAULT '#888888',
    kind      TEXT    NOT NULL DEFAULT 'label' CHECK (kind IN ('label', 'person'))
);

CREATE TABLE IF NOT EXISTS card_labels (
    card_id   INTEGER NOT NULL REFERENCES cards(id)  ON DELETE CASCADE,
    label_id  INTEGER NOT NULL REFERENCES labels(id) ON DELETE CASCADE,
    PRIMARY KEY (card_id, label_id)
);

CREATE TABLE IF NOT EXISTS checklist_items (
    id        INTEGER PRIMARY KEY,
    card_id   INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    text      TEXT    NOT NULL,
    done      INTEGER NOT NULL DEFAULT 0,
    position  REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS lists_by_board       ON lists(board_id, position);
CREATE INDEX IF NOT EXISTS cards_by_list        ON cards(list_id, position);
CREATE INDEX IF NOT EXISTS labels_by_board      ON labels(board_id);
CREATE INDEX IF NOT EXISTS card_labels_by_label ON card_labels(label_id);
CREATE INDEX IF NOT EXISTS checklist_by_card    ON checklist_items(card_id, position);
