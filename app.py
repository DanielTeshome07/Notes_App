"""Notes App — local personal knowledge base with full-text search.

Flask + SQLite FTS5. Stores notes and uploaded documents, splits them into
paragraph-level chunks, and searches across everything down to the paragraph.
"""

import io
import os
import re
import sqlite3
from datetime import datetime

import fitz  # PyMuPDF
from docx import Document as DocxDocument
from flask import Flask, g, jsonify, render_template, request
from werkzeug.utils import secure_filename

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notes.db")
ALLOWED_EXTENSIONS = {"pdf", "docx", "txt"}

app = Flask(__name__)


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
    """Create tables on first run. Safe to call repeatedly."""
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
    db.commit()
    db.close()


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


# Rewrite the FTS3/4-style infix "A NEAR/N B" (which the README advertises and
# users expect) into FTS5's "NEAR(A B, N)" form. A and B may be bare words or
# quoted phrases. Anything else in the query is passed through untouched.
_NEAR_RE = re.compile(r'("[^"]*"|\S+)\s+NEAR/(\d+)\s+("[^"]*"|\S+)')


def translate_near(query):
    return _NEAR_RE.sub(lambda m: f"NEAR({m.group(1)} {m.group(3)}, {m.group(2)})", query)


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


def store_document(title, source_type, paragraphs):
    """Insert a document and its paragraph chunks. Returns the new doc id."""
    db = get_db()
    cur = db.execute(
        "INSERT INTO documents (title, source_type, created_at) VALUES (?, ?, ?)",
        (title, source_type, datetime.utcnow().isoformat(timespec="seconds")),
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


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/add", methods=["POST"])
def add_note():
    data = request.get_json(silent=True) or request.form
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "Note content is empty."}), 400

    title = (data.get("title") or "").strip()
    if not title:
        # First non-empty line, truncated, as the title.
        first_line = next((ln.strip() for ln in content.splitlines() if ln.strip()), "Untitled note")
        title = first_line[:60] + ("…" if len(first_line) > 60 else "")

    paragraphs = split_paragraphs(content)
    if not paragraphs:
        return jsonify({"error": "Note has no searchable text."}), 400

    doc_id = store_document(title, "note", paragraphs)
    return jsonify({"id": doc_id, "title": title, "chunks": len(paragraphs)}), 201


@app.route("/upload", methods=["POST"])
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

    doc_id = store_document(filename, ext, paragraphs)
    return jsonify({"id": doc_id, "title": filename, "chunks": len(paragraphs)}), 201


@app.route("/search")
def search():
    query = (request.args.get("q") or "").strip()
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
            f.chunk_index AS chunk_index,
            highlight(chunks_fts, 0, char(2), char(3)) AS snippet,
            bm25(chunks_fts) AS score
        FROM chunks_fts f
        JOIN documents d ON d.id = f.doc_id
        WHERE chunks_fts MATCH ?
        ORDER BY score
        LIMIT 100
    """
    match_query = translate_near(query)
    db = get_db()
    try:
        rows = db.execute(sql, (match_query,)).fetchall()
    except sqlite3.OperationalError:
        # Query had FTS5 syntax the user didn't intend (stray quote, bare
        # operator, etc.). Retry as a literal phrase of the bare terms.
        terms = re.findall(r"\w+", query)
        if not terms:
            return jsonify({"results": [], "count": 0})
        # Quote each term so FTS5 treats it as a literal — this also neutralises
        # any bare operator keyword (NEAR/AND/OR/NOT) left over from the query.
        fallback = " ".join(f'"{t}"' for t in terms)
        try:
            rows = db.execute(sql, (fallback,)).fetchall()
        except sqlite3.OperationalError:
            return jsonify({"results": [], "count": 0})

    results = [
        {
            "doc_id": r["doc_id"],
            "title": r["title"],
            "source_type": r["source_type"],
            "chunk_index": r["chunk_index"],
            "snippet": r["snippet"],
        }
        for r in rows
    ]
    return jsonify({"results": results, "count": len(results)})


@app.route("/documents")
def list_documents():
    db = get_db()
    rows = db.execute(
        """
        SELECT d.id, d.title, d.source_type, d.created_at,
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
                }
                for r in rows
            ]
        }
    )


@app.route("/delete/<int:doc_id>", methods=["POST", "DELETE"])
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


if __name__ == "__main__":
    init_db()
    app.run(host="127.0.0.1", port=5000, debug=True)
