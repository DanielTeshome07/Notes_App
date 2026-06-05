"""Notes App — personal knowledge base with full-text search.

Flask + SQLite FTS5. Stores notes and uploaded documents, splits them into
paragraph-level chunks, and searches across everything down to the paragraph.

Single-user: when APP_PASSWORD is set, every route is gated behind a password
login. Configuration is read from the environment so the same image runs
locally and in Kubernetes:

    APP_PASSWORD      password required to use the app (unset = no auth, dev only)
    FLASK_SECRET_KEY  session signing key (unset = random, sessions drop on restart)
    DATA_DIR          directory for notes.db (default: app directory)
    MAX_UPLOAD_MB     max upload size in MB (default: 32)
    FLASK_DEBUG       "1" to enable debug (never in production)
"""

import hmac
import io
import os
import re
import sqlite3
from datetime import datetime
from functools import wraps

import fitz  # PyMuPDF
from docx import Document as DocxDocument
from flask import (
    Flask,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
DB_PATH = os.path.join(DATA_DIR, "notes.db")
ALLOWED_EXTENSIONS = {"pdf", "docx", "txt"}
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "32"))

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
AUTH_ENABLED = bool(APP_PASSWORD)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(32)


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
def get_db():
    """Return a per-request SQLite connection."""
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create tables and apply migrations. Safe to call repeatedly."""
    os.makedirs(DATA_DIR, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS documents (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT    NOT NULL,
            source_type TEXT    NOT NULL,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS chunks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id      INTEGER NOT NULL,
            content     TEXT    NOT NULL,
            chunk_index INTEGER NOT NULL,
            FOREIGN KEY (doc_id) REFERENCES documents(id) ON DELETE CASCADE
        );

        -- Standalone FTS5 table mirroring chunks.content. doc_id / chunk_index
        -- are stored unindexed so results can be joined back without a second
        -- lookup. Kept in sync manually on insert/delete (chunks are never
        -- edited in place), which avoids the trigger pitfalls of external
        -- content tables.
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            content,
            doc_id UNINDEXED,
            chunk_index UNINDEXED
        );
        """
    )

    # Migration: tags were added after the initial release. ALTER TABLE so the
    # column appears on databases created before tags existed (e.g. a persisted
    # volume), since CREATE TABLE IF NOT EXISTS won't touch an existing table.
    cols = {row[1] for row in db.execute("PRAGMA table_info(documents)")}
    if "tags" not in cols:
        # Tags are stored sentinel-wrapped (",a,b,") so a tag filter can match
        # exactly with LIKE '%,tag,%' without partial-word false positives.
        db.execute("ALTER TABLE documents ADD COLUMN tags TEXT NOT NULL DEFAULT ','")

    db.commit()
    db.close()


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if AUTH_ENABLED and not session.get("authed"):
            return _auth_challenge()
        return view(*args, **kwargs)

    return wrapped


def _auth_challenge():
    """Redirect browsers to the login page; answer API calls with 401 JSON."""
    wants_html = "text/html" in request.headers.get("Accept", "")
    if request.method == "GET" and wants_html:
        return redirect(url_for("login", next=request.full_path))
    return jsonify({"error": "unauthorized"}), 401


@app.route("/login", methods=["GET", "POST"])
def login():
    if not AUTH_ENABLED or session.get("authed"):
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        supplied = request.form.get("password", "")
        if hmac.compare_digest(supplied, APP_PASSWORD):
            session["authed"] = True
            session.permanent = True
            dest = request.args.get("next") or url_for("index")
            # Only allow same-site relative redirects.
            if not dest.startswith("/"):
                dest = url_for("index")
            return redirect(dest)
        error = "Incorrect password."
    return render_template("login.html", error=error)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# --------------------------------------------------------------------------- #
# Text helpers
# --------------------------------------------------------------------------- #
def split_paragraphs(text):
    """Split raw text into trimmed, non-empty paragraphs.

    Paragraphs are separated by one or more blank lines. Single newlines
    within a paragraph (e.g. wrapped lines) are collapsed to spaces.
    """
    paragraphs = re.split(r"\n\s*\n", text)
    cleaned = []
    for para in paragraphs:
        collapsed = re.sub(r"\s+", " ", para).strip()
        if collapsed:
            cleaned.append(collapsed)
    return cleaned


def normalize_tags(raw):
    """Parse a comma/space separated tag string into a clean, ordered list."""
    if not raw:
        return []
    parts = re.split(r"[,\n]+", raw)
    seen, out = set(), []
    for p in parts:
        tag = re.sub(r"\s+", " ", p).strip().lower()
        if tag and tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out


def pack_tags(tag_list):
    """Sentinel-wrap tags for exact LIKE matching: ['a','b'] -> ',a,b,'."""
    return "," + ",".join(tag_list) + "," if tag_list else ","


def unpack_tags(packed):
    """Inverse of pack_tags: ',a,b,' -> ['a','b']."""
    return [t for t in (packed or ",").split(",") if t]


def extract_pdf(data):
    parts = []
    with fitz.open(stream=data, filetype="pdf") as doc:
        for page in doc:
            parts.append(page.get_text())
    return "\n\n".join(parts)


def extract_docx(data):
    doc = DocxDocument(io.BytesIO(data))
    return "\n\n".join(p.text for p in doc.paragraphs)


def extract_txt(data):
    for encoding in ("utf-8", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def store_document(title, source_type, paragraphs, tags):
    """Insert a document and its paragraph chunks. Returns the new doc id."""
    db = get_db()
    cur = db.execute(
        "INSERT INTO documents (title, source_type, created_at, tags) VALUES (?, ?, ?, ?)",
        (
            title,
            source_type,
            datetime.utcnow().isoformat(timespec="seconds"),
            pack_tags(tags),
        ),
    )
    doc_id = cur.lastrowid
    for index, content in enumerate(paragraphs):
        db.execute(
            "INSERT INTO chunks (doc_id, content, chunk_index) VALUES (?, ?, ?)",
            (doc_id, content, index),
        )
        db.execute(
            "INSERT INTO chunks_fts (content, doc_id, chunk_index) VALUES (?, ?, ?)",
            (content, doc_id, index),
        )
    db.commit()
    return doc_id


# Rewrite the FTS3/4-style infix "A NEAR/N B" (which the README advertises and
# users expect) into FTS5's "NEAR(A B, N)" form. A and B may be bare words or
# quoted phrases. Anything else in the query is passed through untouched.
_NEAR_RE = re.compile(r'("[^"]*"|\S+)\s+NEAR/(\d+)\s+("[^"]*"|\S+)')


def translate_near(query):
    return _NEAR_RE.sub(lambda m: f"NEAR({m.group(1)} {m.group(3)}, {m.group(2)})", query)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/add", methods=["POST"])
@login_required
def add_note():
    data = request.get_json(silent=True) or request.form
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "Note content is empty."}), 400

    title = (data.get("title") or "").strip()
    if not title:
        first_line = next((ln.strip() for ln in content.splitlines() if ln.strip()), "Untitled note")
        title = first_line[:60] + ("…" if len(first_line) > 60 else "")

    paragraphs = split_paragraphs(content)
    if not paragraphs:
        return jsonify({"error": "Note has no searchable text."}), 400

    tags = normalize_tags(data.get("tags"))
    doc_id = store_document(title, "note", paragraphs, tags)
    return jsonify({"id": doc_id, "title": title, "chunks": len(paragraphs), "tags": tags}), 201


@app.route("/upload", methods=["POST"])
@login_required
def upload_file():
    file = request.files.get("file")
    if file is None or not file.filename:
        return jsonify({"error": "No file provided."}), 400

    filename = secure_filename(file.filename)
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({"error": f"Unsupported file type: .{ext or '?'}"}), 400

    data = file.read()
    try:
        if ext == "pdf":
            text = extract_pdf(data)
        elif ext == "docx":
            text = extract_docx(data)
        else:  # txt
            text = extract_txt(data)
    except Exception as exc:  # parsing can fail on malformed files
        return jsonify({"error": f"Could not read file: {exc}"}), 400

    paragraphs = split_paragraphs(text)
    if not paragraphs:
        return jsonify({"error": "No extractable text found in file."}), 400

    tags = normalize_tags(request.form.get("tags"))
    doc_id = store_document(filename, ext, paragraphs, tags)
    return jsonify({"id": doc_id, "title": filename, "chunks": len(paragraphs), "tags": tags}), 201


@app.route("/search")
@login_required
def search():
    query = (request.args.get("q") or "").strip()
    tag_filter = (request.args.get("tag") or "").strip().lower()
    if not query:
        return jsonify({"results": [], "count": 0})

    # \x02 / \x03 are sentinels: highlight() wraps matches with them, and the
    # frontend HTML-escapes the snippet before swapping them for <mark> tags.
    # This keeps highlighting XSS-safe even though chunk content is arbitrary.
    sql = """
        SELECT
            d.id          AS doc_id,
            d.title       AS title,
            d.source_type AS source_type,
            d.tags        AS tags,
            f.chunk_index AS chunk_index,
            highlight(chunks_fts, 0, char(2), char(3)) AS snippet,
            bm25(chunks_fts) AS score
        FROM chunks_fts f
        JOIN documents d ON d.id = f.doc_id
        WHERE chunks_fts MATCH ?
        {tag_clause}
        ORDER BY score
        LIMIT 100
    """
    params = []
    tag_clause = ""
    if tag_filter:
        tag_clause = "AND d.tags LIKE ?"
        # second placeholder filled after the MATCH arg below

    match_query = translate_near(query)

    db = get_db()

    def run(q):
        args = [q]
        if tag_filter:
            args.append(f"%,{tag_filter},%")
        return db.execute(sql.format(tag_clause=tag_clause), args).fetchall()

    try:
        rows = run(match_query)
    except sqlite3.OperationalError:
        # Query had FTS5 syntax the user didn't intend (stray quote, bare
        # operator, etc.). Retry with each bare term quoted as a literal.
        terms = re.findall(r"\w+", query)
        if not terms:
            return jsonify({"results": [], "count": 0})
        fallback = " ".join(f'"{t}"' for t in terms)
        try:
            rows = run(fallback)
        except sqlite3.OperationalError:
            return jsonify({"results": [], "count": 0})

    results = [
        {
            "doc_id": r["doc_id"],
            "title": r["title"],
            "source_type": r["source_type"],
            "chunk_index": r["chunk_index"],
            "snippet": r["snippet"],
            "tags": unpack_tags(r["tags"]),
        }
        for r in rows
    ]
    return jsonify({"results": results, "count": len(results)})


@app.route("/documents")
@login_required
def list_documents():
    db = get_db()
    rows = db.execute(
        """
        SELECT d.id, d.title, d.source_type, d.created_at, d.tags,
               COUNT(c.id) AS chunk_count
        FROM documents d
        LEFT JOIN chunks c ON c.doc_id = d.id
        GROUP BY d.id
        ORDER BY d.created_at DESC, d.id DESC
        """
    ).fetchall()
    return jsonify(
        {
            "documents": [
                {
                    "id": r["id"],
                    "title": r["title"],
                    "source_type": r["source_type"],
                    "created_at": r["created_at"],
                    "chunk_count": r["chunk_count"],
                    "tags": unpack_tags(r["tags"]),
                }
                for r in rows
            ]
        }
    )


@app.route("/tags")
@login_required
def list_tags():
    """Return every distinct tag with a count of documents using it."""
    db = get_db()
    counts = {}
    for (packed,) in db.execute("SELECT tags FROM documents"):
        for tag in unpack_tags(packed):
            counts[tag] = counts.get(tag, 0) + 1
    tags = [{"tag": t, "count": c} for t, c in sorted(counts.items())]
    return jsonify({"tags": tags})


@app.route("/delete/<int:doc_id>", methods=["POST", "DELETE"])
@login_required
def delete_document(doc_id):
    db = get_db()
    exists = db.execute("SELECT 1 FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if not exists:
        return jsonify({"error": "Document not found."}), 404
    db.execute("DELETE FROM chunks_fts WHERE doc_id = ?", (doc_id,))
    db.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
    db.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    db.commit()
    return jsonify({"deleted": doc_id})


@app.route("/healthz")
def healthz():
    """Unauthenticated liveness probe for Kubernetes."""
    return jsonify({"status": "ok"})


# Initialise the schema at import time so it runs under gunicorn too (not just
# the __main__ dev server).
init_db()


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG") == "1"
    app.run(host="127.0.0.1", port=5000, debug=debug)
