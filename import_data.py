"""
import_data.py
Quet tat ca file DM trong DATA_DIR, chi lay ban moi nhat cua moi san pham
(giong update_dinh_muc.py goc), va nap vao SQLite thay vi xuat Excel.

Chay:
    python import_data.py [duong_dan_thu_muc]

Neu khong truyen duong dan, mac dinh quet thu muc ./sample_data
"""
import os
import sys
import glob
from collections import defaultdict
from datetime import datetime

from db import get_conn, init_db, DB_PATH
from extractors import (
    parse_file_meta, ver_num, format_date_str, extract_file,
)


def find_files(data_dir):
    patterns = [
        os.path.join(data_dir, 'DM*.xlsx'), os.path.join(data_dir, 'DM*.xls'),
        os.path.join(data_dir, 'ĐM*.xlsx'), os.path.join(data_dir, 'ĐM*.xls'),
        os.path.join(data_dir, '*', 'DM*.xlsx'), os.path.join(data_dir, '*', 'DM*.xls'),
        os.path.join(data_dir, '*', 'ĐM*.xlsx'), os.path.join(data_dir, '*', 'ĐM*.xls'),
    ]
    return sorted(set(
        f for p in patterns for f in glob.glob(p)
        if 'DINH_MUC_TONG_HOP' not in f and not os.path.basename(f).startswith('~$')
    ))


def select_latest(all_files):
    """Same grouping/versioning rule as the original script: one file per
    base product name, newest by (date, version) wins."""
    groups = defaultdict(list)
    for fp in all_files:
        fname = os.path.basename(fp)
        base_name, date_obj, date_str, ver_str = parse_file_meta(fname)
        groups[base_name].append((fp, date_obj, date_str, ver_str))

    selected, skipped = [], []
    for base_name, variants in sorted(groups.items()):
        variants_sorted = sorted(
            variants, key=lambda x: (x[1] or datetime.min, ver_num(x[3])), reverse=True)
        fp, date_obj, date_str, ver_str = variants_sorted[0]
        label = (ver_str + ' | ' if ver_str else '') + format_date_str(date_str)
        selected.append((fp, label))
        for old_fp, _, old_ds, old_ver in variants_sorted[1:]:
            old_label = (old_ver + ' | ' if old_ver else '') + format_date_str(old_ds)
            skipped.append((os.path.basename(old_fp), old_label))
    return selected, skipped


def get_or_create(conn, table, unique_cols, values):
    """Generic upsert-and-return-id for small lookup tables."""
    where = ' AND '.join(f'{c} IS ?' for c in unique_cols)
    row = conn.execute(f'SELECT id FROM {table} WHERE {where}', values).fetchone()
    if row:
        return row['id']
    cols = ', '.join(unique_cols)
    qmarks = ', '.join('?' for _ in unique_cols)
    cur = conn.execute(f'INSERT INTO {table} ({cols}) VALUES ({qmarks})', values)
    return cur.lastrowid


def upsert_product(conn, ten_sp, ma_sp, source_file, sua_doi_label):
    existing = conn.execute(
        'SELECT id FROM products WHERE ten_sp = ?', (ten_sp,)).fetchone()
    if existing:
        product_id = existing['id']
        conn.execute(
            'UPDATE products SET ma_sp=?, source_file=?, sua_doi_label=?, '
            "imported_at=datetime('now') WHERE id=?",
            (ma_sp, source_file, sua_doi_label, product_id))
        conn.execute('DELETE FROM bom_lines WHERE product_id = ?', (product_id,))
    else:
        cur = conn.execute(
            'INSERT INTO products (ten_sp, ma_sp, source_file, sua_doi_label) '
            'VALUES (?, ?, ?, ?)',
            (ten_sp, ma_sp, source_file, sua_doi_label))
        product_id = cur.lastrowid
    return product_id


def load_rows(conn, product_id, rows):
    for r in rows:
        workshop_id = get_or_create(conn, 'workshops', ['xuong'], [r['Xuong']])
        material_id = get_or_create(
            conn, 'materials', ['ma_vl', 'ten_vl'], [r['Ma_VL'] or None, r['Ten_VL']])
        conn.execute(
            'INSERT INTO bom_lines '
            '(product_id, workshop_id, material_id, quy_cach, dvt, sl_cai, hao_hut, tong_sl, yc_cl) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (product_id, workshop_id, material_id, r['Quy_cach'], r['DVT'],
             r['SL_cai'], r['Hao_hut'], r['Tong_SL'], r['YC_CL']))


def run_import(data_dir):
    if not os.path.exists(DB_PATH):
        init_db()

    print(f"[{datetime.now():%H:%M:%S}] Scanning: {data_dir}")
    all_files = find_files(data_dir)
    print(f'  Found {len(all_files)} DM files')

    selected, skipped = select_latest(all_files)
    for fname, label in skipped:
        print(f'  SKIP (old - {label}): {fname[:55]}')
    if skipped:
        print(f'  -> Excluded {len(skipped)} outdated file(s)\n')

    conn = get_conn()
    total_rows = 0
    try:
        for fp, sua_doi_label in selected:
            fname = os.path.basename(fp)
            try:
                data = extract_file(fp)
            except Exception as e:
                conn.execute(
                    'INSERT INTO import_log (fname, sua_doi, status, detail) VALUES (?,?,?,?)',
                    (fname, sua_doi_label, 'ERROR', str(e)))
                print(f'  ERROR {fname}: {e}')
                continue

            if not data:
                conn.execute(
                    'INSERT INTO import_log (fname, sua_doi, status, row_count) VALUES (?,?,?,0)',
                    (fname, sua_doi_label, 'SKIPPED'))
                print(f'  SKIP (no rows parsed): {fname[:55]}')
                continue

            ten_sp = data[0]['Ten_SP']
            ma_sp = data[0]['Ma_SP']
            product_id = upsert_product(conn, ten_sp, ma_sp, fname, sua_doi_label)
            load_rows(conn, product_id, data)
            conn.execute(
                'INSERT INTO import_log (fname, ten_sp, sua_doi, row_count, status) '
                'VALUES (?,?,?,?,?)',
                (fname, ten_sp, sua_doi_label, len(data), 'OK'))
            total_rows += len(data)
            print(f'  OK [{sua_doi_label}] {fname[:48]}: {len(data)} rows')

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    n_products = len(selected)
    print(f'\n  Total: {total_rows} rows across {n_products} product file(s)')
    print(f"[{datetime.now():%H:%M:%S}] Saved to: {DB_PATH}")


if __name__ == '__main__':
    data_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'sample_data')
    run_import(data_dir)
