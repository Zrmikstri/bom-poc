"""
app.py — FastAPI + Jinja2 + htmx dashboard over the BOM (dinh muc) SQLite DB.
One process serves both the browsable UI and a small JSON API for other systems.

Run:
    uvicorn app:app --reload --host 0.0.0.0 --port 8000

Then open http://localhost:8000  (or http://<this-machine's-IP>:8000 for teammates)
"""
import os
import secrets
import tempfile
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Query, UploadFile, File, Form, Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from db import get_conn, init_db, DB_PATH
from extractors import extract_file, parse_file_meta, ver_num, format_date_str
from import_data import upsert_product, load_rows, get_or_create

load_dotenv()

BOM_USERNAME = os.environ.get('BOM_USERNAME', 'admin')
BOM_PASSWORD = os.environ.get('BOM_PASSWORD', 'changeme')
_security = HTTPBasic()


def verify_credentials(credentials: HTTPBasicCredentials = Depends(_security)):
    user_ok = secrets.compare_digest(credentials.username, BOM_USERNAME)
    pass_ok = secrets.compare_digest(credentials.password, BOM_PASSWORD)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Sai tên đăng nhập hoặc mật khẩu',
            headers={'WWW-Authenticate': 'Basic'},
        )
    return credentials.username

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = FastAPI(title='BOM Dashboard (POC)', dependencies=[Depends(verify_credentials)])
app.mount('/static', StaticFiles(directory=os.path.join(BASE_DIR, 'static')), name='static')
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, 'templates'))

WORKSHOP_COLORS = {
    'XƯỞNG CƠ KHÍ':            '#D6E4F0',
    'XƯỞNG XI-SƠN':             '#FFF2CC',
    'XƯỞNG XI-SƠN (CHUYỀN 01)': '#FFF2CC',
    'XƯỞNG XI-SƠN (CHUYỀN 02)': '#FFE598',
    'XƯỞNG ĐAN':                '#E2EFDA',
    'XƯỞNG NỆM':                '#FCE4D6',
    'XƯỞNG ĐÓNG GÓI':           '#EAD1DC',
    'XƯỞNG NHỰA':               '#E2D9F3',
}
DEFAULT_COLOR = '#F2F2F2'


@app.on_event('startup')
def startup():
    if not os.path.exists(DB_PATH):
        init_db()


def db_stats(conn):
    n_products = conn.execute('SELECT COUNT(*) c FROM products').fetchone()['c']
    n_lines = conn.execute('SELECT COUNT(*) c FROM bom_lines').fetchone()['c']
    n_workshops = conn.execute('SELECT COUNT(*) c FROM workshops').fetchone()['c']
    last_import = conn.execute(
        'SELECT MAX(imported_at) t FROM products').fetchone()['t']
    return dict(n_products=n_products, n_lines=n_lines,
                n_workshops=n_workshops, last_import=last_import)


# ---------- Pages ----------

@app.get('/')
def index(request: Request):
    conn = get_conn()
    workshops = [r['xuong'] for r in
                 conn.execute('SELECT xuong FROM workshops ORDER BY xuong')]
    stats = db_stats(conn)
    conn.close()
    return templates.TemplateResponse(request, 'index.html', {
        'workshops': workshops, 'stats': stats,
    })


@app.get('/search')
def search(request: Request, q: str = '', workshop: str = ''):
    conn = get_conn()
    sql = ('SELECT DISTINCT p.id, p.ten_sp, p.ma_sp, p.sua_doi_label, p.source_file '
           'FROM products p LEFT JOIN bom_lines b ON b.product_id = p.id '
           'LEFT JOIN workshops w ON w.id = b.workshop_id WHERE 1=1')
    params = []
    if q:
        sql += ' AND (p.ten_sp LIKE ? OR p.ma_sp LIKE ?)'
        params += [f'%{q}%', f'%{q}%']
    if workshop:
        sql += ' AND w.xuong = ?'
        params.append(workshop)
    sql += ' ORDER BY p.ten_sp LIMIT 200'
    products = conn.execute(sql, params).fetchall()
    conn.close()
    return templates.TemplateResponse(request, 'partials/product_list.html', {
        'products': products,
    })


@app.get('/products/{product_id}')
def product_detail(request: Request, product_id: int):
    conn = get_conn()
    product = conn.execute('SELECT * FROM products WHERE id = ?', (product_id,)).fetchone()
    lines = conn.execute(
        'SELECT b.*, w.xuong, m.ma_vl, m.ten_vl FROM bom_lines b '
        'LEFT JOIN workshops w ON w.id = b.workshop_id '
        'LEFT JOIN materials m ON m.id = b.material_id '
        'WHERE b.product_id = ? ORDER BY w.xuong, m.ten_vl', (product_id,)).fetchall()
    conn.close()
    return templates.TemplateResponse(request, 'partials/product_detail.html', {
        'product': product, 'lines': lines,
        'colors': WORKSHOP_COLORS, 'default_color': DEFAULT_COLOR,
    })


@app.get('/products/{product_id}/edit')
def product_edit_form(request: Request, product_id: int):
    conn = get_conn()
    product = conn.execute('SELECT * FROM products WHERE id = ?', (product_id,)).fetchone()
    lines = conn.execute(
        'SELECT b.*, w.xuong, m.ma_vl, m.ten_vl FROM bom_lines b '
        'LEFT JOIN workshops w ON w.id = b.workshop_id '
        'LEFT JOIN materials m ON m.id = b.material_id '
        'WHERE b.product_id = ? ORDER BY b.id', (product_id,)).fetchall()
    all_workshops = [r['xuong'] for r in conn.execute('SELECT xuong FROM workshops ORDER BY xuong')]
    conn.close()
    return templates.TemplateResponse(request, 'partials/product_edit.html', {
        'product': product, 'lines': lines, 'all_workshops': all_workshops,
    })


@app.post('/products/{product_id}/edit')
async def product_edit_save(request: Request, product_id: int):
    form = await request.form()
    ten_sp = (form.get('ten_sp') or '').strip()
    ma_sp = (form.get('ma_sp') or '').strip() or None
    sua_doi_label = (form.get('sua_doi_label') or '').strip() or None

    # Parallel arrays: one entry per BOM row still present in the form.
    xuong_l    = form.getlist('xuong')
    ma_vl_l    = form.getlist('ma_vl')
    ten_vl_l   = form.getlist('ten_vl')
    quy_cach_l = form.getlist('quy_cach')
    dvt_l      = form.getlist('dvt')
    sl_cai_l   = form.getlist('sl_cai')
    hao_hut_l  = form.getlist('hao_hut')
    tong_sl_l  = form.getlist('tong_sl')
    yc_cl_l    = form.getlist('yc_cl')

    def to_num(v):
        v = (v or '').strip()
        if v == '':
            return None
        try:
            return float(v)
        except ValueError:
            return None

    conn = get_conn()
    try:
        conn.execute(
            'UPDATE products SET ten_sp=?, ma_sp=?, sua_doi_label=? WHERE id=?',
            (ten_sp, ma_sp, sua_doi_label, product_id))
        conn.execute('DELETE FROM bom_lines WHERE product_id = ?', (product_id,))

        for i in range(len(ten_vl_l)):
            ten_vl = (ten_vl_l[i] or '').strip()
            if not ten_vl:
                continue  # skip fully blank rows
            xuong = (xuong_l[i] or '').strip()
            workshop_id = get_or_create(conn, 'workshops', ['xuong'], [xuong]) if xuong else None
            ma_vl = (ma_vl_l[i] or '').strip() or None
            material_id = get_or_create(
                conn, 'materials', ['ma_vl', 'ten_vl'], [ma_vl, ten_vl])
            conn.execute(
                'INSERT INTO bom_lines '
                '(product_id, workshop_id, material_id, quy_cach, dvt, sl_cai, hao_hut, tong_sl, yc_cl) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (product_id, workshop_id, material_id,
                 (quy_cach_l[i] or '').strip() or None, (dvt_l[i] or '').strip() or None,
                 to_num(sl_cai_l[i]), to_num(hao_hut_l[i]), to_num(tong_sl_l[i]),
                 (yc_cl_l[i] or '').strip() or None))
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        return HTMLResponse(f'<div class="upload-msg err">Lỗi khi lưu: {e}</div>', status_code=400)
    conn.close()

    return product_detail(request, product_id)


@app.post('/upload')
async def upload_file(request: Request, files: list[UploadFile] = File(...)):
    results = []  # [(filename, ok: bool, message: str)]

    for file in files:
        suffix = os.path.splitext(file.filename)[1].lower()
        if suffix not in ('.xlsx', '.xls'):
            results.append((file.filename, False, 'không phải .xlsx/.xls'))
            continue

        # Save with the ORIGINAL filename (in its own temp dir) so the
        # version/date parsing in extract_file/parse_file_meta still works.
        tmp_dir = tempfile.mkdtemp(prefix='bom_upload_')
        tmp_path = os.path.join(tmp_dir, file.filename)
        with open(tmp_path, 'wb') as f:
            f.write(await file.read())

        try:
            data = extract_file(tmp_path)
        except Exception as e:
            results.append((file.filename, False, f'lỗi đọc file: {e}'))
            continue
        finally:
            try:
                os.remove(tmp_path); os.rmdir(tmp_dir)
            except OSError:
                pass

        if not data:
            results.append((file.filename, False,
                             'không đọc được dòng nào (kiểm tra tên sheet "ĐM TH")'))
            continue

        base, date_obj, date_str, ver_str = parse_file_meta(file.filename)
        sua_doi_label = (ver_str + ' | ' if ver_str else '') + format_date_str(date_str)

        conn = get_conn()
        try:
            ten_sp, ma_sp = data[0]['Ten_SP'], data[0]['Ma_SP']
            product_id = upsert_product(conn, ten_sp, ma_sp, file.filename, sua_doi_label)
            load_rows(conn, product_id, data)
            conn.execute(
                'INSERT INTO import_log (fname, ten_sp, sua_doi, row_count, status) VALUES (?,?,?,?,?)',
                (file.filename, ten_sp, sua_doi_label, len(data), 'OK'))
            conn.commit()
            results.append((file.filename, True,
                             f'đã nhập "{ten_sp}" ({len(data)} dòng, {sua_doi_label})'))
        except Exception as e:
            conn.rollback()
            results.append((file.filename, False, f'lỗi khi lưu vào database: {e}'))
        finally:
            conn.close()

    any_ok = any(ok for _, ok, _ in results)
    products = []
    if any_ok:
        conn2 = get_conn()
        products = conn2.execute(
            'SELECT id, ten_sp, ma_sp, sua_doi_label FROM products ORDER BY ten_sp LIMIT 200').fetchall()
        conn2.close()

    return templates.TemplateResponse(request, 'partials/upload_result.html', {
        'results': results, 'any_ok': any_ok, 'products': products,
    })


# ---------- JSON API (for other systems: ERP, reporting, etc.) ----------

@app.get('/api/products')
def api_products(q: str = ''):
    conn = get_conn()
    sql = 'SELECT id, ten_sp, ma_sp, sua_doi_label FROM products'
    params = []
    if q:
        sql += ' WHERE ten_sp LIKE ? OR ma_sp LIKE ?'
        params = [f'%{q}%', f'%{q}%']
    rows = [dict(r) for r in conn.execute(sql, params)]
    conn.close()
    return JSONResponse(rows)


@app.get('/api/products/{product_id}/bom')
def api_product_bom(product_id: int):
    conn = get_conn()
    product = conn.execute('SELECT * FROM products WHERE id = ?', (product_id,)).fetchone()
    if not product:
        return JSONResponse({'error': 'not found'}, status_code=404)
    lines = conn.execute(
        'SELECT w.xuong, m.ma_vl, m.ten_vl, b.quy_cach, b.dvt, b.sl_cai, b.hao_hut, b.tong_sl, b.yc_cl '
        'FROM bom_lines b '
        'LEFT JOIN workshops w ON w.id = b.workshop_id '
        'LEFT JOIN materials m ON m.id = b.material_id '
        'WHERE b.product_id = ?', (product_id,)).fetchall()
    conn.close()
    return JSONResponse({'product': dict(product), 'bom_lines': [dict(l) for l in lines]})


@app.get('/api/materials/{ten_vl}/usage')
def api_material_usage(ten_vl: str):
    """Which products use a given material — handy for 'where-used' lookups."""
    conn = get_conn()
    rows = conn.execute(
        'SELECT p.ten_sp, p.ma_sp, w.xuong, b.sl_cai, b.dvt '
        'FROM bom_lines b '
        'JOIN products p ON p.id = b.product_id '
        'JOIN materials m ON m.id = b.material_id '
        'LEFT JOIN workshops w ON w.id = b.workshop_id '
        'WHERE m.ten_vl LIKE ?', (f'%{ten_vl}%',)).fetchall()
    conn.close()
    return JSONResponse([dict(r) for r in rows])
