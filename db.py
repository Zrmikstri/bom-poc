"""
db.py — SQLite connection helper for the BOM (dinh muc) database.
"""
import os
import sqlite3

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH    = os.path.join(SCRIPT_DIR, 'bom.db')
SCHEMA_PATH = os.path.join(SCRIPT_DIR, 'schema.sql')


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def init_db():
    conn = get_conn()
    with open(SCHEMA_PATH, encoding='utf-8') as f:
        conn.executescript(f.read())
    conn.commit()
    conn.close()


if __name__ == '__main__':
    init_db()
    print(f'Initialized database at {DB_PATH}')
