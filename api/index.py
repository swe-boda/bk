"""
api/index.py - السيرفر الرئيسي (شغال محلي على الجهاز وعلى Vercel Blob أونلاين)
"""

import os
import sys
import threading
import urllib.parse
from functools import wraps

from flask import Flask, jsonify, make_response, render_template, request, send_from_directory

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import stamp_students as S  # noqa: E402

BOOKS_DIR = os.path.join(ROOT, "books")
OUT_DIR = os.path.join(ROOT, "out")
ON_VERCEL = bool(os.environ.get("VERCEL"))
USE_BLOB = bool(os.environ.get("BLOB_READ_WRITE_TOKEN"))
BLOB_MISSING = ON_VERCEL and not USE_BLOB
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

if not USE_BLOB and not ON_VERCEL:
    os.makedirs(BOOKS_DIR, exist_ok=True)
    os.makedirs(OUT_DIR, exist_ok=True)

app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"))
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB محليا


class _VercelPathMiddleware:
    """يرجّع المسار الأصلي لما Vercel يعمل rewrite لـ /api/index."""

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        qs = environ.get("QUERY_STRING", "")
        if "__path=" in qs:
            params = urllib.parse.parse_qsl(qs, keep_blank_values=True)
            real_path = None
            rest = []
            for k, v in params:
                if k == "__path" and real_path is None:
                    real_path = v
                else:
                    rest.append((k, v))
            if real_path:
                if not real_path.startswith("/"):
                    real_path = "/" + real_path
                environ["PATH_INFO"] = urllib.parse.unquote_to_bytes(real_path).decode("latin-1")
                environ["QUERY_STRING"] = urllib.parse.urlencode(rest)
        elif environ.get("PATH_INFO") in ("/api/index", "/api/index.py"):
            environ["PATH_INFO"] = "/"
        return self.wsgi_app(environ, start_response)


app.wsgi_app = _VercelPathMiddleware(app.wsgi_app)

_renderer = None
_tpl_cache = {}
_lock = threading.Lock()


def get_renderer():
    global _renderer
    if _renderer is None:
        with _lock:
            if _renderer is None:
                _renderer = S.NameRenderer(S.FONT_PATH)
    return _renderer


def safe_book_filename(name):
    base = S.safe(os.path.basename(name))
    if not base.lower().endswith(".pdf"):
        base += ".pdf"
    return base


def parse_folder_name(folder):
    if "_" in folder:
        code, name = folder.split("_", 1)
        return code, name
    return "", folder


# ---------------------------------------------------------------- الحماية الاختيارية
def is_authed():
    if not ADMIN_PASSWORD:
        return True
    pw = request.cookies.get("admin_pw") or request.headers.get("X-Admin-Key", "")
    return urllib.parse.unquote(pw) == ADMIN_PASSWORD


def require_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not is_authed():
            return jsonify({"error": "مطلوب تسجيل الدخول الأول", "auth_required": True}), 401
        return fn(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------- التخزين (محلي / Vercel Blob)
def _blob():
    from vercel.blob import sync as blob_sync
    return blob_sync


def list_books():
    if BLOB_MISSING:
        return []
    if USE_BLOB:
        b = _blob()
        items = []
        for item in b.query_blobs(prefix="books/"):
            if not item.pathname.lower().endswith(".pdf"):
                continue
            name = item.pathname[len("books/"):]
            if "/" in name or not name:
                continue
            items.append({
                "name": name,
                "title": os.path.splitext(name)[0],
                "size": item.size,
                "url": item.url,
            })
        return sorted(items, key=lambda x: x["name"])

    items = []
    for f in sorted(os.listdir(BOOKS_DIR)):
        if f.lower().endswith(".pdf"):
            p = os.path.join(BOOKS_DIR, f)
            items.append({
                "name": f,
                "title": os.path.splitext(f)[0],
                "size": os.path.getsize(p),
                "url": "",
            })
    return items


def save_book_local(filename, data):
    fname = safe_book_filename(filename)
    path = os.path.join(BOOKS_DIR, fname)
    with open(path, "wb") as f:
        f.write(data)
    _tpl_cache.pop(fname, None)
    return fname


def delete_book(book_name):
    fname = os.path.basename(book_name)
    _tpl_cache.pop(fname, None)
    if USE_BLOB:
        b = _blob()
        targets = [f"books/{fname}", f"templates/{fname}.tpl"]
        for t in targets:
            try:
                b.delete_blob(t)
            except Exception:
                pass
        return
    p = os.path.join(BOOKS_DIR, fname)
    if os.path.exists(p):
        os.remove(p)


def get_template(book_name):
    fname = os.path.basename(book_name)
    if USE_BLOB:
        with _lock:
            if fname in _tpl_cache:
                return _tpl_cache[fname]
        b = _blob()
        tpl_path = f"templates/{fname}.tpl"
        try:
            res = b.get_blob(tpl_path, access="public")
            tpl = res.body
        except Exception:
            raw = b.get_blob(f"books/{fname}", access="public").body
            tpl = S.build_template(raw)
            try:
                b.put_blob(tpl_path, tpl, access="public", allow_overwrite=True,
                           content_type="application/octet-stream")
            except Exception:
                pass
        with _lock:
            _tpl_cache[fname] = tpl
        return tpl

    path = os.path.join(BOOKS_DIR, fname)
    if not os.path.exists(path):
        raise ValueError("الكتاب ده مش موجود")
    mtime = os.path.getmtime(path)
    with _lock:
        hit = _tpl_cache.get(fname)
        if hit and hit[0] == mtime:
            return hit[1]
    tpl = S.build_template(path)
    with _lock:
        _tpl_cache[fname] = (mtime, tpl)
    return tpl


def save_student_pdf(folder, filename, pdf_bytes):
    if USE_BLOB:
        b = _blob()
        res = b.put_blob(
            f"students/{folder}/{filename}",
            pdf_bytes,
            access="public",
            allow_overwrite=True,
            content_type="application/pdf",
        )
        return res.url

    folder_dir = os.path.join(OUT_DIR, folder)
    os.makedirs(folder_dir, exist_ok=True)
    with open(os.path.join(folder_dir, filename), "wb") as f:
        f.write(pdf_bytes)
    return f"/files/{urllib.parse.quote(folder)}/{urllib.parse.quote(filename)}"


def list_students():
    if BLOB_MISSING:
        return []
    if USE_BLOB:
        b = _blob()
        grouped = {}
        for item in b.query_blobs(prefix="students/"):
            rest = item.pathname[len("students/"):]
            parts = rest.split("/", 1)
            if len(parts) != 2 or not parts[1]:
                continue
            folder, filename = parts
            if not filename.lower().endswith(".pdf"):
                continue
            if folder not in grouped:
                code, name = parse_folder_name(folder)
                grouped[folder] = {
                    "folder": folder,
                    "code": code,
                    "name": name,
                    "sharePath": f"/s/{urllib.parse.quote(folder)}",
                    "files": [],
                    "updatedAt": item.uploaded_at.isoformat() if item.uploaded_at else "",
                }
            grouped[folder]["files"].append({
                "filename": filename,
                "size": item.size,
                "url": item.url,
            })
            if item.uploaded_at and item.uploaded_at.isoformat() > grouped[folder]["updatedAt"]:
                grouped[folder]["updatedAt"] = item.uploaded_at.isoformat()
        res = list(grouped.values())
        for s in res:
            s["files"].sort(key=lambda x: x["filename"])
        return sorted(res, key=lambda x: x["updatedAt"], reverse=True)

    res = []
    if not os.path.exists(OUT_DIR):
        return res
    for folder in os.listdir(OUT_DIR):
        fpath = os.path.join(OUT_DIR, folder)
        if not os.path.isdir(fpath):
            continue
        files = []
        newest = 0
        for fn in sorted(os.listdir(fpath)):
            if not fn.lower().endswith(".pdf"):
                continue
            p = os.path.join(fpath, fn)
            st = os.stat(p)
            newest = max(newest, st.st_mtime)
            files.append({
                "filename": fn,
                "size": st.st_size,
                "url": f"/files/{urllib.parse.quote(folder)}/{urllib.parse.quote(fn)}",
            })
        if files:
            code, name = parse_folder_name(folder)
            res.append({
                "folder": folder,
                "code": code,
                "name": name,
                "sharePath": f"/s/{urllib.parse.quote(folder)}",
                "files": files,
                "updatedAt": newest,
            })
    return sorted(res, key=lambda x: x["updatedAt"], reverse=True)


def get_student_folder(folder):
    folder = S.safe(folder)
    if USE_BLOB:
        b = _blob()
        files = []
        for item in b.query_blobs(prefix=f"students/{folder}/"):
            fn = item.pathname[len(f"students/{folder}/"):]
            if fn.lower().endswith(".pdf") and "/" not in fn:
                files.append({"filename": fn, "size": item.size, "url": item.url})
        files.sort(key=lambda x: x["filename"])
        code, name = parse_folder_name(folder)
        return {"folder": folder, "code": code, "name": name, "files": files}

    fpath = os.path.join(OUT_DIR, folder)
    files = []
    if os.path.isdir(fpath):
        for fn in sorted(os.listdir(fpath)):
            if fn.lower().endswith(".pdf"):
                p = os.path.join(fpath, fn)
                files.append({
                    "filename": fn,
                    "size": os.path.getsize(p),
                    "url": f"/files/{urllib.parse.quote(folder)}/{urllib.parse.quote(fn)}",
                })
    code, name = parse_folder_name(folder)
    return {"folder": folder, "code": code, "name": name, "files": files}


def delete_student_folder(folder):
    folder = S.safe(folder)
    if USE_BLOB:
        b = _blob()
        urls = [item.url for item in b.query_blobs(prefix=f"students/{folder}/")]
        if urls:
            b.delete_blob(urls)
        return
    fpath = os.path.join(OUT_DIR, folder)
    if os.path.isdir(fpath):
        import shutil
        shutil.rmtree(fpath, ignore_errors=True)


# ---------------------------------------------------------------- المسارات (Routes)
@app.get("/")
def home():
    return render_template(
        "index.html",
        use_blob=USE_BLOB,
        blob_missing=BLOB_MISSING,
        auth_required=bool(ADMIN_PASSWORD),
        authed=is_authed(),
    )


@app.get("/api/health")
def api_health():
    return jsonify({
        "ok": True,
        "vercel": ON_VERCEL,
        "use_blob": USE_BLOB,
        "blob_missing": BLOB_MISSING,
        "font_exists": os.path.exists(S.FONT_PATH),
    })


@app.get("/s/<path:folder>")
def student_page(folder):
    info = get_student_folder(urllib.parse.unquote(folder))
    return render_template("student.html", student=info, use_blob=USE_BLOB)


@app.post("/api/auth")
def api_auth():
    data = request.get_json(silent=True) or {}
    pw = data.get("password", "")
    if not ADMIN_PASSWORD or pw == ADMIN_PASSWORD:
        resp = make_response(jsonify({"ok": True}))
        if ADMIN_PASSWORD:
            resp.set_cookie("admin_pw", urllib.parse.quote(pw), httponly=True, samesite="Lax", max_age=86400 * 30)
        return resp
    return jsonify({"error": "كلمة السر غلط"}), 401


@app.get("/api/books")
@require_auth
def api_books():
    return jsonify({"books": list_books(), "mode": "blob" if USE_BLOB else "local"})


@app.post("/api/upload-book")
@require_auth
def api_upload_book():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "اختار ملف PDF الأول"}), 400
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "لازم الملف يكون PDF"}), 400
    data = f.read()
    fname = safe_book_filename(f.filename)
    if USE_BLOB:
        b = _blob()
        b.put_blob(f"books/{fname}", data, access="public", allow_overwrite=True, content_type="application/pdf")
        _tpl_cache.pop(fname, None)
    else:
        save_book_local(fname, data)
    return jsonify({"ok": True, "name": fname})


@app.delete("/api/books/<path:book_name>")
@require_auth
def api_delete_book(book_name):
    delete_book(urllib.parse.unquote(book_name))
    return jsonify({"ok": True})


@app.post("/api/stamp")
@require_auth
def api_stamp():
    data = request.get_json(silent=True) or {}
    book = os.path.basename(data.get("book", "").strip())
    name = data.get("name", "").strip()
    code = data.get("code", "").strip()
    if not book or not name or not code:
        return jsonify({"error": "لازم تكتب الاسم والكود وتختار الكتاب"}), 400
    try:
        tpl = get_template(book)
        pdf_bytes = S.stamp_pdf_bytes(tpl, get_renderer(), name, code)
    except SystemExit as e:
        return jsonify({"error": str(e)}), 400
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": f"حصل خطأ: {e}"}), 500

    folder = f"{S.safe(code)}_{S.safe(name)}"
    book_title = S.safe(os.path.splitext(book)[0])
    filename = f"{book_title}_{S.safe(code)}_{S.safe(name)}.pdf"
    url = save_student_pdf(folder, filename, pdf_bytes)
    return jsonify({
        "ok": True,
        "book": book,
        "bookTitle": book_title,
        "folder": folder,
        "filename": filename,
        "size": len(pdf_bytes),
        "url": url,
        "sharePath": f"/s/{urllib.parse.quote(folder)}",
    })


@app.get("/api/students")
@require_auth
def api_students():
    return jsonify({"students": list_students()})


@app.delete("/api/students/<path:folder>")
@require_auth
def api_delete_student(folder):
    delete_student_folder(urllib.parse.unquote(folder))
    return jsonify({"ok": True})


@app.get("/files/<path:subpath>")
def serve_local_file(subpath):
    if USE_BLOB:
        return jsonify({"error": "not found"}), 404
    subpath = urllib.parse.unquote(subpath)
    return send_from_directory(OUT_DIR, subpath, as_attachment=True)

