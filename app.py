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
import mimetypes
import os
import re
import shutil
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from html.parser import HTMLParser

import fitz  # PyMuPDF
import markdown as md
from docx import Document as DocxDocument
from flask import (
    Flask,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from markupsafe import escape
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
DB_PATH = os.path.join(DATA_DIR, "notes.db")
# Text is extracted (and made searchable) from these upload types.
TEXT_EXTRACTORS = {"pdf", "docx", "txt", "md"}
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "32"))

# Drive storage. Blobs live on disk under DATA_DIR, named by a random key (never
# the user's filename); SQLite only holds metadata.
FILES_DIR = os.path.join(DATA_DIR, "files")
THUMBS_DIR = os.path.join(DATA_DIR, "thumbs")
UPLOADS_DIR = os.path.join(DATA_DIR, "uploads")
# Browsers upload big files in chunks: the Cloudflare tunnel rejects request
# bodies over ~100MB, and streaming chunks to disk keeps pod memory flat.
UPLOAD_CHUNK_MB = int(os.environ.get("UPLOAD_CHUNK_MB", "16"))
MAX_FILE_GB = float(os.environ.get("MAX_FILE_GB", "20"))
EXTRACT_MAX_MB = 25          # skip text extraction above this (memory guard)
DISK_RESERVE_BYTES = 2 * 1024**3   # always leave this much free on the volume
TRASH_DAYS = 30

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
AUTH_ENABLED = bool(APP_PASSWORD)

app = Flask(__name__)
# Caps every request body. Upload chunks must fit under it.
app.config["MAX_CONTENT_LENGTH"] = max(MAX_UPLOAD_MB, UPLOAD_CHUNK_MB + 1) * 1024 * 1024
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(32)

# Types the slim base image's mime table may lack.
for _ext, _type in {
    ".webm": "video/webm", ".mkv": "video/x-matroska", ".mov": "video/quicktime",
    ".m4v": "video/mp4", ".m4a": "audio/mp4", ".opus": "audio/ogg", ".flac": "audio/flac",
    ".heic": "image/heic", ".heif": "image/heif", ".webp": "image/webp", ".avif": "image/avif",
    ".md": "text/markdown", ".yaml": "text/yaml", ".yml": "text/yaml",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}.items():
    mimetypes.add_type(_type, _ext)


@app.context_processor
def asset_version():
    """Cache-buster for static assets: browsers refetch whenever one changes."""
    v = 0
    for name in ("style.css", "drive.css", "drive.js"):
        try:
            v = max(v, int(os.path.getmtime(os.path.join(BASE_DIR, "static", name))))
        except OSError:
            pass
    return {"css_version": v}


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
def get_db():
    """Return a per-request SQLite connection."""
    if "db" not in g:
        # gunicorn runs several threads; WAL (set in init_db) lets reads run
        # alongside a write, and busy_timeout waits out a concurrent writer.
        g.db = sqlite3.connect(DB_PATH, timeout=15)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA busy_timeout = 15000")
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

    # Migration: the editor stores the raw, exact text as the source of truth.
    # Chunks remain a derived search index. Rows created before this column
    # existed have content = NULL and fall back to reconstructing from chunks.
    if "content" not in cols:
        db.execute("ALTER TABLE documents ADD COLUMN content TEXT")

    # Migration: how a document's content should be rendered on export/preview.
    # 'plain' (the default for every pre-existing row, including uploaded
    # PDF/DOCX/TXT) renders verbatim line-by-line; 'markdown' renders the
    # editor's Markdown. Defaulting old rows to 'plain' keeps their exports
    # byte-for-byte identical to before this feature.
    if "format" not in cols:
        db.execute(
            "ALTER TABLE documents ADD COLUMN format TEXT NOT NULL DEFAULT 'plain'"
        )

    # Migration: track the last edit separately from creation, so the library
    # can show "edited N ago" and sort by recency. Pre-existing rows inherit
    # their created_at as a best guess.
    if "updated_at" not in cols:
        db.execute("ALTER TABLE documents ADD COLUMN updated_at TIMESTAMP")
        db.execute("UPDATE documents SET updated_at = created_at WHERE updated_at IS NULL")

    # Migration: pinned documents float to the top of the library.
    if "pinned" not in cols:
        db.execute("ALTER TABLE documents ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")

    # Migration (Drive): notes live in folders, can be trashed, and an uploaded
    # PDF/DOCX/TXT keeps its original file — the document row then holds only
    # its searchable text and points at the file via file_id.
    for col in ("folder_id", "file_id", "trashed_at"):
        if col not in cols:
            kind = "TIMESTAMP" if col == "trashed_at" else "INTEGER"
            db.execute(f"ALTER TABLE documents ADD COLUMN {col} {kind}")

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS folders (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL,
            parent_id   INTEGER REFERENCES folders(id),
            starred     INTEGER NOT NULL DEFAULT 0,
            created_at  TIMESTAMP,
            updated_at  TIMESTAMP,
            trashed_at  TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS files (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            folder_id   INTEGER REFERENCES folders(id),
            name        TEXT    NOT NULL,
            mime        TEXT    NOT NULL,
            size        INTEGER NOT NULL,
            storage_key TEXT    NOT NULL UNIQUE,
            has_thumb   INTEGER NOT NULL DEFAULT 0,
            starred     INTEGER NOT NULL DEFAULT 0,
            created_at  TIMESTAMP,
            updated_at  TIMESTAMP,
            trashed_at  TIMESTAMP
        );

        -- In-progress chunked uploads; the bytes accumulate in UPLOADS_DIR/<id>.
        CREATE TABLE IF NOT EXISTS uploads (
            id          TEXT    PRIMARY KEY,
            name        TEXT    NOT NULL,
            size        INTEGER NOT NULL,
            received    INTEGER NOT NULL DEFAULT 0,
            folder_id   INTEGER,
            tags        TEXT    NOT NULL DEFAULT ',',
            created_at  TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_folders_parent ON folders(parent_id);
        CREATE INDEX IF NOT EXISTS idx_files_folder ON files(folder_id);
        CREATE INDEX IF NOT EXISTS idx_documents_folder ON documents(folder_id);
        CREATE INDEX IF NOT EXISTS idx_documents_file ON documents(file_id);
        """
    )
    # Persistent for the DB file: concurrent readers don't block on a writer.
    db.execute("PRAGMA journal_mode=WAL")

    db.commit()
    db.close()
    for d in (FILES_DIR, THUMBS_DIR, UPLOADS_DIR):
        os.makedirs(d, exist_ok=True)


def utcnow_iso():
    """Naive-UTC ISO timestamp, matching the format of existing rows."""
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


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
            # Only allow same-site relative redirects. "//host" and "/\host"
            # are protocol-relative and would leave the site, so reject any
            # second slash/backslash too.
            if not re.match(r"^/(?![/\\])", dest):
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


def _index_chunks(db, doc_id, paragraphs):
    """(Re)build the chunk + FTS rows for a document. Caller commits."""
    db.execute("DELETE FROM chunks_fts WHERE doc_id = ?", (doc_id,))
    db.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
    for index, content in enumerate(paragraphs):
        db.execute(
            "INSERT INTO chunks (doc_id, content, chunk_index) VALUES (?, ?, ?)",
            (doc_id, content, index),
        )
        db.execute(
            "INSERT INTO chunks_fts (content, doc_id, chunk_index) VALUES (?, ?, ?)",
            (content, doc_id, index),
        )


def store_document(title, source_type, content, paragraphs, tags, fmt="plain"):
    """Insert a document, its raw content, and its paragraph chunks.

    `content` is the verbatim source of truth (preserves line breaks);
    `paragraphs` is the derived, whitespace-collapsed search index;
    `fmt` is 'plain' or 'markdown' (how content renders on export).
    Returns the new doc id.
    """
    db = get_db()
    now = utcnow_iso()
    cur = db.execute(
        "INSERT INTO documents (title, source_type, created_at, updated_at, tags, content, format) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (title, source_type, now, now, pack_tags(tags), content, fmt),
    )
    doc_id = cur.lastrowid
    _index_chunks(db, doc_id, paragraphs)
    db.commit()
    return doc_id


def update_document(doc_id, title, content, paragraphs, tags, fmt="plain"):
    """Overwrite a document's title/content/tags/format and rebuild its index.

    Leaves created_at and source_type untouched.
    """
    db = get_db()
    db.execute(
        "UPDATE documents SET title = ?, content = ?, tags = ?, format = ?, updated_at = ? WHERE id = ?",
        (title, content, pack_tags(tags), fmt, utcnow_iso(), doc_id),
    )
    _index_chunks(db, doc_id, paragraphs)
    db.commit()


def document_content(row):
    """Return a document's full text.

    Prefers the verbatim `content` column; falls back to reconstructing from
    chunks for rows created before that column existed.
    """
    if row["content"] is not None:
        return row["content"]
    db = get_db()
    chunks = db.execute(
        "SELECT content FROM chunks WHERE doc_id = ? ORDER BY chunk_index",
        (row["id"],),
    ).fetchall()
    return "\n\n".join(c["content"] for c in chunks)


# --------------------------------------------------------------------------- #
# Markdown rendering
#
# A single Markdown->HTML engine feeds the editor Preview, the print view, and
# both binary exports, so what you see in Preview is what the PDF and .docx
# render. The supported subset is deliberately small and matched to the editor
# toolbar: headings, bold, italic, bullet/numbered lists, blockquote, links,
# inline code, and horizontal rules.
# --------------------------------------------------------------------------- #
VALID_FORMATS = ("plain", "markdown")
_MD_EXTENSIONS = ["sane_lists", "nl2br"]
_ALLOWED_TAGS = {
    "h1", "h2", "h3", "p", "strong", "em", "ul", "ol", "li",
    "blockquote", "a", "code", "pre", "br", "hr",
}


class _Sanitizer(HTMLParser):
    """Rebuild HTML keeping only an allowlist of tags; escape everything else.

    Markdown can pass raw HTML through, so the rendered output is filtered to a
    safe subset before it ever reaches the page or a renderer.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []

    def handle_starttag(self, tag, attrs):
        if tag in ("br", "hr"):
            self.out.append(f"<{tag}/>")
        elif tag == "a":
            href = dict(attrs).get("href", "") or ""
            if re.match(r"^(https?:|mailto:)", href, re.I):
                self.out.append(
                    f'<a href="{escape(href)}" rel="noopener noreferrer" target="_blank">'
                )
            else:
                self.out.append("<a>")
        elif tag in _ALLOWED_TAGS:
            self.out.append(f"<{tag}>")

    def handle_startendtag(self, tag, attrs):
        if tag in ("br", "hr"):
            self.out.append(f"<{tag}/>")

    def handle_endtag(self, tag):
        if tag in _ALLOWED_TAGS and tag not in ("br", "hr"):
            self.out.append(f"</{tag}>")

    def handle_data(self, data):
        self.out.append(str(escape(data)))


def markdown_to_html(content):
    """Render Markdown to sanitized HTML (shared by preview, print, exports)."""
    raw = md.markdown(content or "", extensions=_MD_EXTENSIONS)
    s = _Sanitizer()
    s.feed(raw)
    s.close()
    return "".join(s.out)


class _TextExtractor(HTMLParser):
    """Flatten rendered HTML to plain text with blank lines between blocks."""

    _BLOCK = {"p", "h1", "h2", "h3", "li", "blockquote", "pre"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_startendtag(self, tag, attrs):
        if tag in ("br", "hr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._BLOCK:
            self.parts.append("\n\n")

    def handle_data(self, data):
        self.parts.append(data)


def markdown_to_text(content):
    """Strip Markdown to readable plain text (for the search index/snippets)."""
    e = _TextExtractor()
    e.feed(markdown_to_html(content))
    e.close()
    return "".join(e.parts)


def index_text(content, fmt):
    """The text a document is searched on: Markdown is flattened, plain is verbatim."""
    return markdown_to_text(content) if fmt == "markdown" else content


class _DocxBuilder(HTMLParser):
    """Walk sanitized HTML and emit python-docx paragraphs/runs."""

    BLOCK_STYLE = {"h1": "Heading 1", "h2": "Heading 2", "h3": "Heading 3",
                   "p": None, "blockquote": "Quote"}

    def __init__(self, doc):
        super().__init__(convert_charrefs=True)
        self.doc = doc
        self.p = None
        self.bold = self.italic = self.code = False
        self.lists = []

    def _new_para(self, style=None):
        try:
            self.p = self.doc.add_paragraph(style=style)
        except KeyError:  # style missing from the default template
            self.p = self.doc.add_paragraph()

    def handle_starttag(self, tag, attrs):
        if tag in ("ul", "ol"):
            self.lists.append(tag)
        elif tag == "li":
            self._new_para("List Number" if self.lists[-1:] == ["ol"] else "List Bullet")
        elif tag in self.BLOCK_STYLE:
            self._new_para(self.BLOCK_STYLE[tag])
        elif tag == "strong":
            self.bold = True
        elif tag == "em":
            self.italic = True
        elif tag == "code":
            self.code = True

    def handle_startendtag(self, tag, attrs):
        if tag == "br" and self.p is not None:
            self.p.add_run().add_break()
        elif tag == "hr":
            self.doc.add_paragraph()

    def handle_endtag(self, tag):
        if tag in ("ul", "ol"):
            if self.lists:
                self.lists.pop()
        elif tag == "li" or tag in self.BLOCK_STYLE:
            self.p = None
        elif tag == "strong":
            self.bold = False
        elif tag == "em":
            self.italic = False
        elif tag == "code":
            self.code = False

    def handle_data(self, data):
        if self.p is None:
            if not data.strip():
                return
            self._new_para()
        run = self.p.add_run(data)
        run.bold = self.bold
        run.italic = self.italic
        if self.code:
            run.font.name = "Courier New"


def render_pdf(title, content, fmt):
    """Render title + content to a PDF and return the bytes (PyMuPDF Story)."""
    if fmt == "markdown":
        body = markdown_to_html(content)
        css = "font-family:serif;font-size:12pt;line-height:1.5"
        head = f"<h1>{escape(title)}</h1>"
    else:  # plain: verbatim, one paragraph per line (unchanged legacy output)
        body = "".join(f"<p>{escape(line)}</p>" for line in content.split("\n"))
        css = "font-family:sans-serif;font-size:11pt;line-height:1.5"
        head = f"<h2>{escape(title)}</h2>"
    html = f"<html><body style='{css}'>{head}{body}</body></html>"
    story = fitz.Story(html=html)
    buf = io.BytesIO()
    writer = fitz.DocumentWriter(buf)
    mediabox = fitz.paper_rect("a4")
    where = mediabox + (54, 54, -54, -54)  # ~0.75in margins
    more = 1
    while more:
        dev = writer.begin_page(mediabox)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()
    buf.seek(0)
    return buf


def render_docx(title, content, fmt):
    """Render title + content to a .docx and return a BytesIO."""
    doc = DocxDocument()
    doc.add_heading(title, level=1)
    if fmt == "markdown":
        _DocxBuilder(doc).feed(markdown_to_html(content))
    else:  # plain: one paragraph per line (unchanged legacy output)
        for line in content.split("\n"):
            doc.add_paragraph(line)
    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf


# Rewrite the FTS3/4-style infix "A NEAR/N B" (which the README advertises and
# users expect) into FTS5's "NEAR(A B, N)" form. A and B may be bare words or
# quoted phrases. Anything else in the query is passed through untouched.
_NEAR_RE = re.compile(r'("[^"]*"|\S+)\s+NEAR/(\d+)\s+("[^"]*"|\S+)')


def translate_near(query):
    return _NEAR_RE.sub(lambda m: f"NEAR({m.group(1)} {m.group(3)}, {m.group(2)})", query)


def _parse_format(data):
    """Read and validate a 'format' field, defaulting to 'plain'."""
    fmt = (data.get("format") or "plain").strip().lower()
    return fmt if fmt in VALID_FORMATS else "plain"


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/editor")
@app.route("/editor/<int:doc_id>")
@login_required
def editor_page(doc_id=None):
    """Full-screen writing page. /editor is a blank new file; /editor/<id>
    edits an existing document (its text is fetched client-side)."""
    return render_template("editor.html", doc_id=doc_id)


@app.route("/add", methods=["POST"])
@login_required
def add_note():
    data = request.get_json(silent=True) or request.form
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "Note content is empty."}), 400

    fmt = _parse_format(data)
    searchable = index_text(content, fmt)
    paragraphs = split_paragraphs(searchable)
    if not paragraphs:
        return jsonify({"error": "Note has no searchable text."}), 400

    title = (data.get("title") or "").strip()
    if not title:
        first_line = next((ln.strip() for ln in searchable.splitlines() if ln.strip()), "Untitled note")
        title = first_line[:60] + ("…" if len(first_line) > 60 else "")

    tags = normalize_tags(data.get("tags"))
    doc_id = store_document(title, "note", content, paragraphs, tags, fmt)
    folder_id = _live_folder_id(get_db(), data.get("folder_id"))
    if folder_id is not None:
        get_db().execute("UPDATE documents SET folder_id = ? WHERE id = ?", (folder_id, doc_id))
        get_db().commit()
    return jsonify({"id": doc_id, "title": title, "chunks": len(paragraphs), "tags": tags,
                    "format": fmt, "folder_id": folder_id}), 201


@app.route("/upload", methods=["POST"])
@login_required
def upload_file():
    """Single-request upload of any file (small files / API clients).

    The browser UI uses the chunked /api/uploads flow instead, which also
    handles files bigger than one request body may be.
    """
    file = request.files.get("file")
    if file is None or not file.filename:
        return jsonify({"error": "No file provided."}), 400
    os.makedirs(UPLOADS_DIR, exist_ok=True)
    part = os.path.join(UPLOADS_DIR, uuid.uuid4().hex + ".single")
    with open(part, "wb") as out:
        shutil.copyfileobj(file.stream, out, 1024 * 1024)
    db = get_db()
    folder_id = _live_folder_id(db, request.form.get("folder_id"))
    tags = normalize_tags(request.form.get("tags"))
    try:
        info = finalize_file(db, part, file.filename, folder_id, tags)
    finally:
        if os.path.exists(part):
            os.remove(part)
    return jsonify(info), 201


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
            d.folder_id   AS folder_id,
            d.file_id     AS file_id,
            d.trashed_at  AS trashed_at,
            fl.folder_id  AS file_folder_id,
            fl.trashed_at AS file_trashed_at,
            fl.mime       AS file_mime,
            f.chunk_index AS chunk_index,
            highlight(chunks_fts, 0, char(2), char(3)) AS snippet,
            bm25(chunks_fts) AS score
        FROM chunks_fts f
        JOIN documents d ON d.id = f.doc_id
        LEFT JOIN files fl ON fl.id = d.file_id
        WHERE chunks_fts MATCH ?
        {tag_clause}
        ORDER BY score
        LIMIT 200
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

    # Hide anything in the trash: the doc itself, its file, or any folder above.
    dead = _dead_folder_ids(db)

    def alive(r):
        if r["trashed_at"]:
            return False
        if r["file_id"] is not None:
            return r["file_trashed_at"] is None and r["file_folder_id"] not in dead
        return r["folder_id"] not in dead

    results = [
        {
            "doc_id": r["doc_id"],
            "file_id": r["file_id"],
            "kind": file_kind(r["file_mime"]) if r["file_mime"] else None,
            "title": r["title"],
            "source_type": r["source_type"],
            "chunk_index": r["chunk_index"],
            "snippet": r["snippet"],
            "tags": unpack_tags(r["tags"]),
        }
        for r in rows
        if alive(r)
    ][:100]
    return jsonify({"results": results, "count": len(results)})


@app.route("/documents")
@login_required
def list_documents():
    db = get_db()
    rows = db.execute(
        """
        SELECT d.id, d.title, d.source_type, d.created_at, d.updated_at,
               d.tags, d.pinned,
               COUNT(c.id) AS chunk_count,
               (SELECT substr(content, 1, 160) FROM chunks
                WHERE doc_id = d.id ORDER BY chunk_index LIMIT 1) AS excerpt
        FROM documents d
        LEFT JOIN chunks c ON c.doc_id = d.id
        WHERE d.file_id IS NULL AND d.trashed_at IS NULL
        GROUP BY d.id
        ORDER BY d.pinned DESC,
                 COALESCE(d.updated_at, d.created_at) DESC, d.id DESC
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
                    "updated_at": r["updated_at"] or r["created_at"],
                    "chunk_count": r["chunk_count"],
                    "tags": unpack_tags(r["tags"]),
                    "pinned": bool(r["pinned"]),
                    "excerpt": r["excerpt"] or "",
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
    for (packed,) in db.execute("SELECT tags FROM documents WHERE trashed_at IS NULL"):
        for tag in unpack_tags(packed):
            counts[tag] = counts.get(tag, 0) + 1
    tags = [{"tag": t, "count": c} for t, c in sorted(counts.items())]
    return jsonify({"tags": tags})


@app.route("/document/<int:doc_id>")
@login_required
def get_document(doc_id):
    """Return one document with its full, verbatim text for the editor."""
    db = get_db()
    row = db.execute(
        "SELECT id, title, source_type, created_at, tags, content, format, folder_id "
        "FROM documents WHERE id = ?",
        (doc_id,),
    ).fetchone()
    if row is None:
        return jsonify({"error": "Document not found."}), 404
    return jsonify(
        {
            "id": row["id"],
            "title": row["title"],
            "source_type": row["source_type"],
            "created_at": row["created_at"],
            "tags": unpack_tags(row["tags"]),
            "content": document_content(row),
            "format": row["format"],
            "folder_id": row["folder_id"],
        }
    )


@app.route("/document/<int:doc_id>", methods=["POST", "PUT"])
@login_required
def save_document(doc_id):
    """Update an existing document's title, content, and tags."""
    db = get_db()
    exists = db.execute("SELECT 1 FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if not exists:
        return jsonify({"error": "Document not found."}), 404

    data = request.get_json(silent=True) or request.form
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "Document is empty."}), 400

    fmt = _parse_format(data)
    searchable = index_text(content, fmt)
    paragraphs = split_paragraphs(searchable)
    if not paragraphs:
        return jsonify({"error": "Document has no searchable text."}), 400

    title = (data.get("title") or "").strip()
    if not title:
        first_line = next(
            (ln.strip() for ln in searchable.splitlines() if ln.strip()), "Untitled"
        )
        title = first_line[:60] + ("…" if len(first_line) > 60 else "")

    tags = normalize_tags(data.get("tags"))
    update_document(doc_id, title, content, paragraphs, tags, fmt)
    return jsonify({"id": doc_id, "title": title, "chunks": len(paragraphs), "tags": tags, "format": fmt})


@app.route("/document/<int:doc_id>/pin", methods=["POST"])
@login_required
def toggle_pin(doc_id):
    """Toggle a document's pinned flag; pinned docs float to the library top."""
    db = get_db()
    row = db.execute("SELECT pinned FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Document not found."}), 404
    pinned = 0 if row["pinned"] else 1
    db.execute("UPDATE documents SET pinned = ? WHERE id = ?", (pinned, doc_id))
    db.commit()
    return jsonify({"id": doc_id, "pinned": bool(pinned)})


@app.route("/render", methods=["POST"])
@login_required
def render_markdown():
    """Render Markdown to sanitized HTML for the editor Preview and print view."""
    data = request.get_json(silent=True) or {}
    return jsonify({"html": markdown_to_html(data.get("content") or "")})


# Renderers and MIME types per export format. PDF/DOCX build binary streams;
# TXT is encoded inline below.
_EXPORTERS = {
    "pdf": ("application/pdf", render_pdf),
    "docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        render_docx,
    ),
    "txt": ("text/plain; charset=utf-8", None),
}


@app.route("/export/<int:doc_id>/<fmt>")
@login_required
def export_document(doc_id, fmt):
    """Download a document as PDF, DOCX, or TXT."""
    if fmt not in _EXPORTERS:
        return jsonify({"error": f"Unsupported format: {fmt}"}), 400

    db = get_db()
    row = db.execute(
        "SELECT id, title, content, format FROM documents WHERE id = ?", (doc_id,)
    ).fetchone()
    if row is None:
        return jsonify({"error": "Document not found."}), 404

    title = row["title"]
    content = document_content(row)
    doc_format = row["format"]
    mimetype, renderer = _EXPORTERS[fmt]
    # TXT is always the raw source; PDF/DOCX render per the document's format.
    stream = io.BytesIO(content.encode("utf-8")) if renderer is None else renderer(title, content, doc_format)

    stem = secure_filename(os.path.splitext(title)[0]) or f"document-{doc_id}"
    return send_file(
        stream,
        mimetype=mimetype,
        as_attachment=True,
        download_name=f"{stem}.{fmt}",
    )


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


# --------------------------------------------------------------------------- #
# Drive: folders + any-file storage
#
# Three item kinds share one folder tree: folders, files (blobs on disk) and
# notes (editor documents). Deleting moves an item to the trash (trashed_at);
# children of a trashed folder are hidden with it and come back on restore.
# "Delete forever" removes rows and blobs.
# --------------------------------------------------------------------------- #
# Served inline (in the page / preview). Everything else is download-only, so
# an uploaded .html or .svg can never run script on this origin.
INLINE_MIMES = {
    "image/jpeg", "image/png", "image/gif", "image/webp", "image/avif", "image/bmp",
    "video/mp4", "video/webm", "video/ogg", "video/quicktime",
    "audio/mpeg", "audio/mp4", "audio/ogg", "audio/wav", "audio/x-wav", "audio/wave",
    "audio/webm", "audio/flac", "audio/aac",
    "application/pdf",
}
TEXT_PREVIEW_MAX = 2 * 1024 * 1024
_TEXTISH_EXT = {
    "txt", "md", "csv", "log", "json", "yaml", "yml", "xml", "ini", "conf", "toml",
    "py", "js", "ts", "sh", "go", "rs", "java", "c", "h", "cpp", "css", "sql", "env",
    "dockerfile", "tf", "html", "svg",
}
_SANDBOX_CSP = "sandbox; default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'"
KINDS = ("folder", "file", "note")


def file_kind(mime, name=""):
    """Coarse category used for icons, previews and filters."""
    mime = mime or ""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    if mime == "application/pdf":
        return "pdf"
    if "wordprocessing" in mime or ext in ("doc", "docx", "odt", "rtf"):
        return "doc"
    if "spreadsheet" in mime or ext in ("xls", "xlsx", "ods", "csv"):
        return "sheet"
    if "presentation" in mime or ext in ("ppt", "pptx", "odp"):
        return "slides"
    if ext in ("zip", "tar", "gz", "tgz", "bz2", "xz", "7z", "rar"):
        return "archive"
    if mime.startswith("text/") or ext in _TEXTISH_EXT:
        return "text"
    return "other"


def clean_name(raw, fallback="Untitled"):
    """Display name: no path separators or control characters, max 255 chars."""
    name = re.sub(r"[\x00-\x1f\x7f/\\]", "", str(raw or "")).strip()
    return name[:255] or fallback


def _int_or_none(v):
    if v in (None, "", "null", "root"):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _dead_folder_ids(db):
    """Ids of trashed folders plus every folder beneath one."""
    rows = db.execute("SELECT id, parent_id, trashed_at FROM folders").fetchall()
    children = {}
    for r in rows:
        children.setdefault(r["parent_id"], []).append(r["id"])
    dead = set()
    stack = [r["id"] for r in rows if r["trashed_at"]]
    while stack:
        fid = stack.pop()
        if fid in dead:
            continue
        dead.add(fid)
        stack.extend(children.get(fid, []))
    return dead


def _live_folder_id(db, raw):
    """Parse a folder id; None (root) if missing, unknown or in the trash."""
    fid = _int_or_none(raw)
    if fid is None:
        return None
    row = db.execute("SELECT id FROM folders WHERE id = ?", (fid,)).fetchone()
    if row is None or fid in _dead_folder_ids(db):
        return None
    return fid


def _breadcrumbs(db, fid):
    path, seen = [], set()
    while fid is not None and fid not in seen:
        seen.add(fid)
        row = db.execute("SELECT id, name, parent_id FROM folders WHERE id = ?", (fid,)).fetchone()
        if row is None:
            break
        path.append({"id": row["id"], "name": row["name"]})
        fid = row["parent_id"]
    return list(reversed(path))


def _subtree_ids(db, fid):
    """fid and all folders beneath it."""
    out, stack = set(), [fid]
    while stack:
        cur = stack.pop()
        if cur in out:
            continue
        out.add(cur)
        stack.extend(r[0] for r in db.execute("SELECT id FROM folders WHERE parent_id = ?", (cur,)))
    return out


def folder_json(r, extra=None):
    d = {"type": "folder", "id": r["id"], "name": r["name"], "parent_id": r["parent_id"],
         "starred": bool(r["starred"]), "created_at": r["created_at"],
         "updated_at": r["updated_at"] or r["created_at"], "trashed_at": r["trashed_at"]}
    if extra:
        d.update(extra)
    return d


def file_json(r):
    return {"type": "file", "id": r["id"], "name": r["name"], "mime": r["mime"],
            "kind": file_kind(r["mime"], r["name"]), "size": r["size"],
            "folder_id": r["folder_id"], "starred": bool(r["starred"]),
            "has_thumb": bool(r["has_thumb"]), "inline": r["mime"] in INLINE_MIMES,
            "created_at": r["created_at"], "updated_at": r["updated_at"] or r["created_at"],
            "trashed_at": r["trashed_at"]}


def note_json(r):
    return {"type": "note", "id": r["id"], "name": r["title"], "source_type": r["source_type"],
            "folder_id": r["folder_id"], "starred": bool(r["pinned"]),
            "tags": unpack_tags(r["tags"]), "excerpt": r["excerpt"] or "",
            "created_at": r["created_at"], "updated_at": r["updated_at"] or r["created_at"],
            "trashed_at": r["trashed_at"]}


_NOTE_COLS = """d.id, d.title, d.source_type, d.folder_id, d.pinned, d.tags, d.created_at,
    d.updated_at, d.trashed_at,
    (SELECT substr(content, 1, 160) FROM chunks WHERE doc_id = d.id
     ORDER BY chunk_index LIMIT 1) AS excerpt"""


# ---- thumbnails / text extraction ---------------------------------------- #
def make_thumb(src, key, mime):
    """Write a small JPEG preview for images and PDFs. Returns True on success."""
    out = os.path.join(THUMBS_DIR, key + ".jpg")
    try:
        from PIL import Image, ImageOps

        if mime.startswith("image/") and mime != "image/svg+xml":
            with Image.open(src) as im:
                im.draft("RGB", (960, 960))        # JPEG: decode at reduced size
                im = ImageOps.exif_transpose(im)   # phone photos: honour rotation
                im.thumbnail((480, 480))
                if im.mode in ("RGBA", "LA", "P"):
                    im = im.convert("RGBA")
                    bg = Image.new("RGB", im.size, (255, 255, 255))
                    bg.paste(im, mask=im.split()[-1])
                    im = bg
                im.convert("RGB").save(out, "JPEG", quality=80)
            return True
        if mime == "application/pdf":
            with fitz.open(src) as doc:
                if not doc.page_count:
                    return False
                page = doc[0]
                zoom = 480 / max(page.rect.width, page.rect.height, 1)
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                Image.frombytes("RGB", (pix.width, pix.height), pix.samples).save(
                    out, "JPEG", quality=80)
            return True
    except Exception:  # unsupported/corrupt image (e.g. HEIC): icon fallback
        if os.path.exists(out):
            os.remove(out)
    return False


def _extract_text_for(path, ext):
    with open(path, "rb") as f:
        data = f.read()
    if ext == "pdf":
        return extract_pdf(data)
    if ext == "docx":
        return extract_docx(data)
    return extract_txt(data)


def finalize_file(db, part_path, raw_name, folder_id, tags):
    """Move a fully received upload into storage and register it.

    PDF/DOCX/TXT/MD also get their text indexed (as a document row linked by
    file_id) so full-text search covers them, as before Drive existed.
    """
    name = clean_name(raw_name, "file")
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    key = uuid.uuid4().hex
    dest = os.path.join(FILES_DIR, key)
    os.makedirs(FILES_DIR, exist_ok=True)
    os.replace(part_path, dest)
    size = os.path.getsize(dest)
    has_thumb = make_thumb(dest, key, mime)
    now = utcnow_iso()
    cur = db.execute(
        "INSERT INTO files (folder_id, name, mime, size, storage_key, has_thumb, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (folder_id, name, mime, size, key, int(has_thumb), now, now),
    )
    file_id = cur.lastrowid
    db.commit()

    indexed = 0
    if ext in TEXT_EXTRACTORS and size <= EXTRACT_MAX_MB * 1024 * 1024:
        try:
            text = _extract_text_for(dest, ext)
            paragraphs = split_paragraphs(text)
            if paragraphs:
                doc_id = store_document(name, ext, text, paragraphs, tags)
                db.execute("UPDATE documents SET file_id = ?, folder_id = ? WHERE id = ?",
                           (file_id, folder_id, doc_id))
                db.commit()
                indexed = len(paragraphs)
        except Exception:  # unreadable PDF etc.: keep the file, just not searchable
            pass
    row = db.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    info = file_json(row)
    info["indexed_paragraphs"] = indexed
    return info


# ---- permanent deletion / housekeeping ----------------------------------- #
def _purge_file(db, file_id):
    row = db.execute("SELECT storage_key FROM files WHERE id = ?", (file_id,)).fetchone()
    if row is None:
        return
    for doc in db.execute("SELECT id FROM documents WHERE file_id = ?", (file_id,)).fetchall():
        _purge_note(db, doc["id"])
    db.execute("DELETE FROM files WHERE id = ?", (file_id,))
    for p in (os.path.join(FILES_DIR, row["storage_key"]),
              os.path.join(THUMBS_DIR, row["storage_key"] + ".jpg")):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


def _purge_note(db, doc_id):
    db.execute("DELETE FROM chunks_fts WHERE doc_id = ?", (doc_id,))
    db.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
    db.execute("DELETE FROM documents WHERE id = ?", (doc_id,))


def _purge_folder(db, folder_id):
    ids = _subtree_ids(db, folder_id)
    marks = ",".join("?" * len(ids))
    for r in db.execute(f"SELECT id FROM files WHERE folder_id IN ({marks})", list(ids)).fetchall():
        _purge_file(db, r["id"])
    for r in db.execute(
        f"SELECT id FROM documents WHERE folder_id IN ({marks}) AND file_id IS NULL", list(ids)
    ).fetchall():
        _purge_note(db, r["id"])
    # Children before parents so the parent_id foreign key never dangles.
    depth = {fid: len(_breadcrumbs(db, fid)) for fid in ids}
    for fid in sorted(ids, key=lambda f: -depth[f]):
        db.execute("DELETE FROM folders WHERE id = ?", (fid,))


def _purge(db, kind, item_id):
    {"folder": _purge_folder, "file": _purge_file, "note": _purge_note}[kind](db, item_id)


_TABLE = {"folder": "folders", "file": "files", "note": "documents"}


def housekeeping(db):
    """Empty trash older than TRASH_DAYS and drop abandoned partial uploads."""
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None)
              - timedelta(days=TRASH_DAYS)).isoformat(timespec="seconds")
    for kind, table in _TABLE.items():
        for r in db.execute(
            f"SELECT id FROM {table} WHERE trashed_at IS NOT NULL AND trashed_at < ?", (cutoff,)
        ).fetchall():
            if db.execute(f"SELECT 1 FROM {table} WHERE id = ?", (r["id"],)).fetchone():
                _purge(db, kind, r["id"])
    stale = (datetime.now(timezone.utc).replace(tzinfo=None)
             - timedelta(days=2)).isoformat(timespec="seconds")
    for r in db.execute("SELECT id FROM uploads WHERE created_at < ?", (stale,)).fetchall():
        _drop_upload(db, r["id"])
    db.commit()


def _drop_upload(db, upload_id):
    db.execute("DELETE FROM uploads WHERE id = ?", (upload_id,))
    try:
        os.remove(os.path.join(UPLOADS_DIR, upload_id + ".part"))
    except FileNotFoundError:
        pass


# ---- pages ---------------------------------------------------------------- #
@app.route("/api/drive")
@login_required
def drive_list():
    """Contents of one folder (root when ?folder is empty)."""
    db = get_db()
    fid = _int_or_none(request.args.get("folder"))
    if fid is not None:
        exists = db.execute("SELECT 1 FROM folders WHERE id = ?", (fid,)).fetchone()
        if not exists or fid in _dead_folder_ids(db):
            return jsonify({"error": "Folder not found."}), 404
    folders = db.execute(
        """SELECT f.*, (SELECT COUNT(*) FROM folders c WHERE c.parent_id = f.id AND c.trashed_at IS NULL)
                      + (SELECT COUNT(*) FROM files c WHERE c.folder_id = f.id AND c.trashed_at IS NULL)
                      + (SELECT COUNT(*) FROM documents c WHERE c.folder_id = f.id
                           AND c.trashed_at IS NULL AND c.file_id IS NULL) AS item_count
           FROM folders f WHERE f.parent_id IS ? AND f.trashed_at IS NULL""",
        (fid,),
    ).fetchall()
    files = db.execute(
        "SELECT * FROM files WHERE folder_id IS ? AND trashed_at IS NULL", (fid,)
    ).fetchall()
    notes = db.execute(
        f"SELECT {_NOTE_COLS} FROM documents d "
        "WHERE d.folder_id IS ? AND d.trashed_at IS NULL AND d.file_id IS NULL",
        (fid,),
    ).fetchall()
    return jsonify({
        "folder": fid,
        "breadcrumbs": _breadcrumbs(db, fid),
        "items": [folder_json(r, {"item_count": r["item_count"]}) for r in folders]
                 + [file_json(r) for r in files] + [note_json(r) for r in notes],
    })


def _alive_items(db, where_files, where_notes, where_folders=None, args=(), limit=200):
    dead = _dead_folder_ids(db)
    items = []
    if where_folders:
        for r in db.execute(f"SELECT * FROM folders WHERE trashed_at IS NULL AND {where_folders}", args):
            if r["id"] not in dead:
                items.append(folder_json(r))
    for r in db.execute(f"SELECT * FROM files WHERE trashed_at IS NULL AND {where_files}", args):
        if r["folder_id"] not in dead:
            items.append(file_json(r))
    for r in db.execute(
        f"SELECT {_NOTE_COLS} FROM documents d WHERE d.trashed_at IS NULL AND d.file_id IS NULL "
        f"AND {where_notes}", args
    ):
        if r["folder_id"] not in dead:
            items.append(note_json(r))
    items.sort(key=lambda i: i["updated_at"] or "", reverse=True)
    return items[:limit]


@app.route("/api/drive/recent")
@login_required
def drive_recent():
    db = get_db()
    return jsonify({"items": _alive_items(db, "1", "1", limit=60)})


@app.route("/api/drive/starred")
@login_required
def drive_starred():
    db = get_db()
    return jsonify({"items": _alive_items(db, "starred = 1", "d.pinned = 1", "starred = 1")})


@app.route("/api/drive/tag")
@login_required
def drive_tag():
    """Notes and indexed uploads carrying one tag (tags are kept on documents)."""
    tag = (request.args.get("t") or "").strip().lower()
    if not tag:
        return jsonify({"items": []})
    db = get_db()
    like = f"%,{tag},%"
    items = _alive_items(db, "id IN (SELECT file_id FROM documents WHERE file_id IS NOT NULL AND tags LIKE ?)",
                         "d.tags LIKE ?", args=(like,), limit=500)
    return jsonify({"items": items})


@app.route("/api/drive/search")
@login_required
def drive_search():
    """Name search across folders, files and notes (content search is /search)."""
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"items": []})
    like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    db = get_db()
    items = _alive_items(
        db, "name LIKE ? ESCAPE '\\'", "d.title LIKE ? ESCAPE '\\'", "name LIKE ? ESCAPE '\\'",
        args=(like,), limit=100,
    )
    return jsonify({"items": items})


@app.route("/api/drive/trash")
@login_required
def drive_trash():
    db = get_db()
    housekeeping(db)
    items = [folder_json(r) for r in db.execute("SELECT * FROM folders WHERE trashed_at IS NOT NULL")]
    items += [file_json(r) for r in db.execute("SELECT * FROM files WHERE trashed_at IS NOT NULL")]
    items += [note_json(r) for r in db.execute(
        f"SELECT {_NOTE_COLS} FROM documents d WHERE d.trashed_at IS NOT NULL AND d.file_id IS NULL")]
    items.sort(key=lambda i: i["trashed_at"] or "", reverse=True)
    return jsonify({"items": items, "retention_days": TRASH_DAYS})


@app.route("/api/drive/trash/empty", methods=["POST"])
@login_required
def drive_empty_trash():
    db = get_db()
    n = 0
    for kind, table in _TABLE.items():
        for r in db.execute(f"SELECT id FROM {table} WHERE trashed_at IS NOT NULL").fetchall():
            if db.execute(f"SELECT 1 FROM {table} WHERE id = ?", (r["id"],)).fetchone():
                _purge(db, kind, r["id"])
                n += 1
    db.commit()
    return jsonify({"purged": n})


@app.route("/api/storage")
@login_required
def drive_storage():
    db = get_db()
    used, count = db.execute("SELECT COALESCE(SUM(size), 0), COUNT(*) FROM files").fetchone()
    by_kind = {}
    for r in db.execute("SELECT mime, name, size FROM files"):
        k = file_kind(r["mime"], r["name"])
        by_kind[k] = by_kind.get(k, 0) + r["size"]
    disk = shutil.disk_usage(DATA_DIR)
    return jsonify({"used": used, "files": count, "by_kind": by_kind,
                    "disk_total": disk.total, "disk_free": disk.free,
                    "max_file_bytes": int(MAX_FILE_GB * 1024**3)})


# ---- item mutations -------------------------------------------------------- #
@app.route("/api/folders", methods=["POST"])
@login_required
def create_folder():
    data = request.get_json(silent=True) or {}
    db = get_db()
    parent = _live_folder_id(db, data.get("parent_id"))
    if _int_or_none(data.get("parent_id")) is not None and parent is None:
        return jsonify({"error": "Parent folder not found."}), 404
    name = clean_name(data.get("name"), "New folder")
    # Reuse an existing same-named folder when asked (used by folder uploads).
    if data.get("reuse"):
        row = db.execute(
            "SELECT * FROM folders WHERE parent_id IS ? AND name = ? AND trashed_at IS NULL",
            (parent, name),
        ).fetchone()
        if row:
            return jsonify(folder_json(row))
    now = utcnow_iso()
    cur = db.execute(
        "INSERT INTO folders (name, parent_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (name, parent, now, now),
    )
    db.commit()
    row = db.execute("SELECT * FROM folders WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(folder_json(row)), 201


def _get_item(db, kind, item_id):
    if kind not in _TABLE:
        return None
    return db.execute(f"SELECT * FROM {_TABLE[kind]} WHERE id = ?", (item_id,)).fetchone()


@app.route("/api/item/<kind>/<int:item_id>", methods=["PATCH"])
@login_required
def update_item(kind, item_id):
    """Rename, move (folder_id) and/or star an item."""
    db = get_db()
    row = _get_item(db, kind, item_id)
    if row is None:
        return jsonify({"error": "Not found."}), 404
    data = request.get_json(silent=True) or {}
    table = _TABLE[kind]
    sets, args = [], []

    if "name" in data:
        name = clean_name(data["name"], "")
        if not name:
            return jsonify({"error": "Name can't be empty."}), 400
        sets.append("title = ?" if kind == "note" else "name = ?")
        args.append(name)
        if kind == "file":   # keep the linked search document's title in sync
            db.execute("UPDATE documents SET title = ? WHERE file_id = ?", (name, item_id))

    if "folder_id" in data:
        target = _int_or_none(data["folder_id"])
        if target is not None and _live_folder_id(db, target) is None:
            return jsonify({"error": "Destination folder not found."}), 404
        if kind == "folder" and target is not None and target in _subtree_ids(db, item_id):
            return jsonify({"error": "Can't move a folder into itself."}), 400
        sets.append("parent_id = ?" if kind == "folder" else "folder_id = ?")
        args.append(target)
        if kind == "file":
            db.execute("UPDATE documents SET folder_id = ? WHERE file_id = ?", (target, item_id))

    if "starred" in data:
        sets.append("pinned = ?" if kind == "note" else "starred = ?")
        args.append(1 if data["starred"] else 0)

    if not sets:
        return jsonify({"error": "Nothing to change."}), 400
    if "name" in data or "folder_id" in data:
        sets.append("updated_at = ?")
        args.append(utcnow_iso())
    db.execute(f"UPDATE {table} SET {', '.join(sets)} WHERE id = ?", (*args, item_id))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/item/<kind>/<int:item_id>/trash", methods=["POST"])
@login_required
def trash_item(kind, item_id):
    db = get_db()
    if _get_item(db, kind, item_id) is None:
        return jsonify({"error": "Not found."}), 404
    db.execute(f"UPDATE {_TABLE[kind]} SET trashed_at = ? WHERE id = ?", (utcnow_iso(), item_id))
    db.commit()
    return jsonify({"trashed": True})


@app.route("/api/item/<kind>/<int:item_id>/restore", methods=["POST"])
@login_required
def restore_item(kind, item_id):
    db = get_db()
    row = _get_item(db, kind, item_id)
    if row is None:
        return jsonify({"error": "Not found."}), 404
    parent_col = "parent_id" if kind == "folder" else "folder_id"
    parent = row[parent_col]
    # If its folder is gone or still in the trash, restore to the root.
    if parent is not None and _live_folder_id(db, parent) is None:
        parent = None
    db.execute(f"UPDATE {_TABLE[kind]} SET trashed_at = NULL, {parent_col} = ? WHERE id = ?",
               (parent, item_id))
    if kind == "file":
        db.execute("UPDATE documents SET folder_id = ? WHERE file_id = ?", (parent, item_id))
    db.commit()
    return jsonify({"restored": True, "folder_id": parent})


@app.route("/api/item/<kind>/<int:item_id>", methods=["DELETE"])
@login_required
def delete_item_forever(kind, item_id):
    db = get_db()
    if _get_item(db, kind, item_id) is None:
        return jsonify({"error": "Not found."}), 404
    _purge(db, kind, item_id)
    db.commit()
    return jsonify({"deleted": True})


# ---- chunked uploads ------------------------------------------------------- #
@app.route("/api/uploads", methods=["POST"])
@login_required
def upload_init():
    data = request.get_json(silent=True) or {}
    db = get_db()
    try:
        size = int(data.get("size"))
    except (TypeError, ValueError):
        return jsonify({"error": "Missing file size."}), 400
    if size < 0:
        return jsonify({"error": "Bad file size."}), 400
    if size > MAX_FILE_GB * 1024**3:
        return jsonify({"error": f"File is larger than the {MAX_FILE_GB:g} GB limit."}), 413
    if shutil.disk_usage(DATA_DIR).free - size < DISK_RESERVE_BYTES:
        return jsonify({"error": "Not enough free space on the server."}), 507
    raw_folder = data.get("folder_id")
    folder_id = _live_folder_id(db, raw_folder)
    if _int_or_none(raw_folder) is not None and folder_id is None:
        return jsonify({"error": "Folder not found."}), 404
    upload_id = uuid.uuid4().hex
    db.execute(
        "INSERT INTO uploads (id, name, size, folder_id, tags, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (upload_id, clean_name(data.get("name"), "file"), size, folder_id,
         pack_tags(normalize_tags(data.get("tags"))), utcnow_iso()),
    )
    db.commit()
    open(os.path.join(UPLOADS_DIR, upload_id + ".part"), "wb").close()
    return jsonify({"upload_id": upload_id, "chunk_size": UPLOAD_CHUNK_MB * 1024 * 1024}), 201


def _get_upload(db, upload_id):
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id or ""):
        return None
    return db.execute("SELECT * FROM uploads WHERE id = ?", (upload_id,)).fetchone()


@app.route("/api/uploads/<upload_id>", methods=["GET"])
@login_required
def upload_status(upload_id):
    up = _get_upload(get_db(), upload_id)
    if up is None:
        return jsonify({"error": "Upload not found."}), 404
    return jsonify({"upload_id": up["id"], "size": up["size"], "received": up["received"]})


@app.route("/api/uploads/<upload_id>", methods=["PUT"])
@login_required
def upload_chunk(upload_id):
    """Append one chunk (raw request body) at ?offset=N.

    The offset must equal the bytes received so far, so a retried chunk can't
    be written twice; on mismatch the client re-syncs from `received`.
    """
    db = get_db()
    up = _get_upload(db, upload_id)
    if up is None:
        return jsonify({"error": "Upload not found."}), 404
    offset = _int_or_none(request.args.get("offset"))
    part = os.path.join(UPLOADS_DIR, upload_id + ".part")
    on_disk = os.path.getsize(part) if os.path.exists(part) else 0
    if offset != on_disk:
        return jsonify({"error": "Offset mismatch.", "received": on_disk}), 409
    with open(part, "r+b") as f:
        f.seek(offset)
        shutil.copyfileobj(request.stream, f, 1024 * 1024)
        f.truncate()
        received = f.tell()
    if received > up["size"]:
        _drop_upload(db, upload_id)
        db.commit()
        return jsonify({"error": "More data than the declared size."}), 400
    db.execute("UPDATE uploads SET received = ? WHERE id = ?", (received, upload_id))
    db.commit()
    return jsonify({"received": received})


@app.route("/api/uploads/<upload_id>/complete", methods=["POST"])
@login_required
def upload_complete(upload_id):
    db = get_db()
    up = _get_upload(db, upload_id)
    if up is None:
        return jsonify({"error": "Upload not found."}), 404
    part = os.path.join(UPLOADS_DIR, upload_id + ".part")
    received = os.path.getsize(part) if os.path.exists(part) else -1
    if received != up["size"]:
        return jsonify({"error": "Upload incomplete.", "received": received}), 409
    # The folder may have been trashed while the upload ran.
    folder_id = _live_folder_id(db, up["folder_id"])
    info = finalize_file(db, part, up["name"], folder_id, unpack_tags(up["tags"]))
    db.execute("DELETE FROM uploads WHERE id = ?", (upload_id,))
    db.commit()
    return jsonify(info), 201


@app.route("/api/uploads/<upload_id>", methods=["DELETE"])
@login_required
def upload_cancel(upload_id):
    db = get_db()
    if _get_upload(db, upload_id) is not None:
        _drop_upload(db, upload_id)
        db.commit()
    return jsonify({"cancelled": True})


# ---- serving files --------------------------------------------------------- #
def _file_row(file_id):
    return get_db().execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()


def _harden(resp, sandbox=True):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cache-Control"] = "private, max-age=0, must-revalidate"
    if sandbox:
        resp.headers["Content-Security-Policy"] = _SANDBOX_CSP
    return resp


@app.route("/files/<int:file_id>/view")
@login_required
def file_view(file_id):
    """Inline, Range-capable (video seeking) view for safe media types.

    Text-like files are served as text/plain so the browser never renders
    them as HTML; anything else falls back to a download.
    """
    row = _file_row(file_id)
    if row is None:
        return jsonify({"error": "File not found."}), 404
    path = os.path.join(FILES_DIR, row["storage_key"])
    if row["mime"] in INLINE_MIMES:
        resp = send_file(path, mimetype=row["mime"], conditional=True,
                         download_name=row["name"])
        # Chrome's PDF viewer refuses to load inside a CSP-sandboxed document.
        return _harden(resp, sandbox=row["mime"] != "application/pdf")
    if file_kind(row["mime"], row["name"]) == "text":
        resp = send_file(path, mimetype="text/plain", conditional=True,
                         download_name=row["name"])
        return _harden(resp)
    return file_download(file_id)


@app.route("/files/<int:file_id>/download")
@login_required
def file_download(file_id):
    row = _file_row(file_id)
    if row is None:
        return jsonify({"error": "File not found."}), 404
    resp = send_file(os.path.join(FILES_DIR, row["storage_key"]), mimetype=row["mime"],
                     as_attachment=True, download_name=row["name"], conditional=True)
    return _harden(resp)


@app.route("/files/<int:file_id>/thumb")
@login_required
def file_thumb(file_id):
    row = _file_row(file_id)
    if row is None or not row["has_thumb"]:
        return jsonify({"error": "No thumbnail."}), 404
    resp = send_file(os.path.join(THUMBS_DIR, row["storage_key"] + ".jpg"),
                     mimetype="image/jpeg", conditional=True)
    resp = _harden(resp)
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


@app.route("/api/file/<int:file_id>")
@login_required
def file_info(file_id):
    row = _file_row(file_id)
    if row is None:
        return jsonify({"error": "File not found."}), 404
    return jsonify(file_json(row))


@app.route("/files/<int:file_id>/text")
@login_required
def file_text(file_id):
    """Text for the preview pane: raw text files, or extracted text (DOCX…)."""
    row = _file_row(file_id)
    if row is None:
        return jsonify({"error": "File not found."}), 404
    doc = get_db().execute(
        "SELECT id, content FROM documents WHERE file_id = ?", (file_id,)).fetchone()
    if doc is not None and doc["content"]:
        return jsonify({"text": doc["content"][:TEXT_PREVIEW_MAX], "source": "extracted"})
    if file_kind(row["mime"], row["name"]) == "text" and row["size"] <= TEXT_PREVIEW_MAX:
        with open(os.path.join(FILES_DIR, row["storage_key"]), "rb") as f:
            return jsonify({"text": extract_txt(f.read()), "source": "raw"})
    return jsonify({"text": None})


@app.route("/healthz")
def healthz():
    """Unauthenticated liveness probe for Kubernetes."""
    return jsonify({"status": "ok"})


# Initialise the schema at import time so it runs under gunicorn too (not just
# the __main__ dev server).
init_db()
with app.app_context():
    housekeeping(get_db())


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG") == "1"
    app.run(host="127.0.0.1", port=5000, debug=debug)
