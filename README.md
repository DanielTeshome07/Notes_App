# Notes App — Personal Knowledge Base with Full-Text Search

A lightweight, fully local web app to store notes, PDFs, and documents — and find anything instantly by searching down to the paragraph level.

## The Problem It Solves

You have notes scattered across files. You remember a phrase or idea but can't find which document it's in. This app lets you dump everything in one place and search across all of it in one shot.

## How It Works

1. Run `python app.py`
2. Open your browser at `http://localhost:5000`
3. Write a note in the editor (with a Markdown formatting toolbar), or upload a PDF/Word/text file
4. Search for any word or phrase — results show the exact matching paragraph, highlighted, with the source document name
5. Open any document to read, edit, or export it as PDF or Word

Everything is stored locally in a SQLite database (`notes.db`). No account, no internet, no cloud.

## Features

- **Rich editor** — a full-page, Docs-style writing surface with a Markdown formatting toolbar (headings, bold/italic, lists, links, code), live preview, autosave, a word-count status bar, and a distraction-free focus mode
- **Upload files** — PDF, DOCX, TXT supported (drag-and-drop or click)
- **Per-document format tracking** — typed notes can be Markdown; uploaded and plain docs stay verbatim. A single Markdown→HTML engine drives the preview, print view, and both exports so they always match
- **Export to PDF & Word** — download any document as a `.pdf` or `.docx`
- **Paragraph-level search** — finds the exact section of a document, not just the file name
- **Phrase search** — search `"exact phrase"` to match words in order
- **Highlighted results** — matching words are highlighted in the result snippet
- **Tags / collections** — label documents, then filter search and the list by tag
- **Dark mode** — toggle that remembers your choice and respects your system preference
- **Password protection** — optional single-password gate so it can run on the internet for just you
- **Delete documents** — remove notes or files you no longer need

## Tech Stack

| Layer | Technology | Why |
|-------|-----------|-----|
| Backend | Python + Flask | Simple, minimal, runs locally |
| Search | SQLite FTS5 | Built into Python's sqlite3 — no extra install, fast phrase search |
| PDF parsing & export | PyMuPDF (`fitz`) | Accurate text extraction from PDFs, and rendering documents back out to PDF |
| DOCX parsing & export | python-docx | Reads and writes Word documents |
| Markdown | `Markdown` | Renders notes to HTML for preview, print, and exports |
| Frontend | Plain HTML/CSS/JS | No framework needed — fast and simple |
| Fonts | Self-hosted Inter | Bundled `.woff2` files — no external CDN call |

## Project Structure

```
notes-app/
├── app.py              # Flask app: routes, auth, database setup, file parsing
├── requirements.txt    # Python dependencies
├── Dockerfile          # Container image (gunicorn)
├── .dockerignore
├── .gitignore
├── README.md
├── templates/
│   ├── index.html      # Home: app bar, search, upload, document grid
│   ├── editor.html     # Full-page editor: Markdown toolbar, preview, autosave
│   └── login.html      # Password gate
├── static/
│   ├── style.css       # Styling (light + dark)
│   └── fonts/          # Self-hosted Inter (.woff2)
└── deploy/             # k3s + Cloudflare manifests and deploy guide
    ├── noteapp.yaml
    ├── secret.example.yaml
    └── README.md
```

## Database Design

SQLite with FTS5 (Full-Text Search version 5):

```
documents
  id          INTEGER PRIMARY KEY
  title       TEXT          -- file name or first line of note
  source_type TEXT          -- 'note', 'pdf', 'docx', 'txt'
  created_at  TIMESTAMP
  content     TEXT          -- full document text (for editing and export)
  tags        TEXT          -- packed tag list
  format      TEXT          -- 'plain' or 'markdown' (how content renders/exports)

chunks
  id          INTEGER PRIMARY KEY
  doc_id      INTEGER       -- references documents.id
  content     TEXT          -- one paragraph of text
  chunk_index INTEGER       -- order within the document

chunks_fts    (FTS5 virtual table)
  mirrors chunks.content for fast full-text search
  supports phrase queries, prefix queries, NEAR queries
```

Documents are split into paragraphs at index time. Search queries the FTS5 table and returns matching chunks with their source document info.

## Setup (on your main machine)

```bash
git clone https://github.com/YOUR_USERNAME/notes-app.git
cd notes-app
pip install -r requirements.txt
python app.py
```

Then open `http://localhost:5000` in your browser.

### Configuration (environment variables)

All optional — with none set, the app runs locally with no password (as before).

| Variable | Default | Purpose |
|----------|---------|---------|
| `APP_PASSWORD` | _(unset)_ | If set, every route is gated behind this password. Unset = no auth (local use). |
| `FLASK_SECRET_KEY` | random | Signs the login session. Set a fixed value so logins survive restarts. |
| `DATA_DIR` | app dir | Where `notes.db` lives (a mounted volume in production). |
| `MAX_UPLOAD_MB` | `32` | Maximum upload size. |
| `FLASK_DEBUG` | off | `1` enables the dev debugger — never in production. |

For production, run under a real server instead of the Flask dev server:

```bash
APP_PASSWORD='your-password' FLASK_SECRET_KEY="$(openssl rand -hex 32)" \
  gunicorn -b 0.0.0.0:5000 -w 1 app:app
```

## Deploying to the internet

The app is containerized and ships with k3s manifests to run it privately
(password-gated, HTTPS) behind Traefik + Cloudflare at your own subdomain. See
[`deploy/README.md`](deploy/README.md) for the full build → push → apply → DNS
walkthrough.

## Search Tips

| Query | What it finds |
|-------|--------------|
| `project deadline` | paragraphs containing both words anywhere |
| `"project deadline"` | that exact phrase in that order |
| `meet*` | meeting, meets, meeting — prefix wildcard |
| `deadline NEAR/3 budget` | deadline and budget within 3 words of each other |

## Roadmap

- [x] Tag/label documents for grouping
- [x] Dark mode
- [x] Drag-and-drop file upload
- [x] Password protection + deploy to k3s/Cloudflare
- [x] Rich editor with Markdown formatting toolbar
- [x] Export documents to PDF and Word
- [x] CI/CD pipeline — push to `main` auto-builds + deploys ([`.github/workflows/deploy.yml`](.github/workflows/deploy.yml))
- [ ] Folder/collection support
