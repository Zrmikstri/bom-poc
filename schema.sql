-- schema.sql
-- Database: keeps only the LATEST version of each product's dinh muc (BOM),
-- matching the behavior of the original update_dinh_muc.py script.

PRAGMA journal_mode = WAL;   -- lets readers (dashboard) work while an import writes
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS products (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ma_sp         TEXT,
    ten_sp        TEXT NOT NULL UNIQUE,   -- dedup key, same as script's grouping key
    source_file   TEXT,
    sua_doi_label TEXT,                   -- e.g. "V2 | 15/03/26"
    imported_at   TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS workshops (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    xuong TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS materials (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ma_vl  TEXT,
    ten_vl TEXT NOT NULL,
    UNIQUE (ma_vl, ten_vl)
);

CREATE TABLE IF NOT EXISTS bom_lines (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id   INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    workshop_id  INTEGER REFERENCES workshops(id),
    material_id  INTEGER REFERENCES materials(id),
    quy_cach     TEXT,
    dvt          TEXT,
    sl_cai       REAL,
    hao_hut      REAL,
    tong_sl      REAL,
    yc_cl        TEXT
);

CREATE TABLE IF NOT EXISTS import_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fname       TEXT,
    ten_sp      TEXT,
    sua_doi     TEXT,
    row_count   INTEGER,
    status      TEXT,          -- OK / SKIPPED / ERROR
    detail      TEXT,
    imported_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_bom_lines_product   ON bom_lines(product_id);
CREATE INDEX IF NOT EXISTS idx_bom_lines_workshop   ON bom_lines(workshop_id);
CREATE INDEX IF NOT EXISTS idx_bom_lines_material   ON bom_lines(material_id);
CREATE INDEX IF NOT EXISTS idx_products_ten_sp      ON products(ten_sp);
CREATE INDEX IF NOT EXISTS idx_materials_ten_vl     ON materials(ten_vl);
