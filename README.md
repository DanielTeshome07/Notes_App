# Notes App — Personal Knowledge Base with Full-Text Search

A lightweight, fully local web app to store notes, PDFs, and documents — and find anything instantly by searching down to the paragraph level.

## The Problem It Solves

You have notes scattered across files. You remember a phrase or idea but can't find which document it's in. This app lets you dump everything in one place and search across all of it in one shot.

## How It Works

1. Run `python app.py`
2. Open your browser at `http://localhost:5000`
3. Add notes by typing/pasting, or upload a PDF/Word/text file
4. Search for any word or phrase — results show the exact matching paragraph, highlighted, with the source document name

Everything is stored locally in a SQLite database (`notes.db`). No account, no internet, no cloud.

## Features

- **Add text notes** — paste anything directly into the app
- **Upload files** — PDF, DOCX, TXT supported
- **Paragraph-level search** — finds the exact section of a document, not just the file name
- **Phrase search** — search `"exact phrase"` to match words in order
- **Highlighted results** — matching words are highlighted in the result snippet
- **Delete documents** — remove notes or files you no longer need

## Tech Stack

| Layer | Technology | Why |
|-------|-----------|-----|
| Backend | Python + Flask | Simple, minimal, runs locally |
| Search | SQLite FTS5 | Built into Python's sqlite3 — no extra install, fast phrase search |
| PDF parsing | PyMuPDF (`fitz`) | Accurate text + layout extraction from PDFs |
| DOCX parsing | python-docx | Extracts text from Word documents |
| Frontend | Plain HTML/CSS/JS | No framework needed — fast and simple |

## Project Structure

```
notes-app/
├── app.py              # Flask app: routes, database setup, file parsing
├── requirements.txt    # Python dependencies
├── .gitignore
├── README.md
├── templates/
│   └── index.html      # Single-page UI (search bar, upload, results)
└── static/
    └── style.css       # Minimal styling
```

## Database Design

SQLite with FTS5 (Full-Text Search version 5):

```
documents
  id          INTEGER PRIMARY KEY
  title       TEXT          -- file name or first line of note
  source_type TEXT          -- 'note', 'pdf', 'docx', 'txt'
  created_at  TIMESTAMP

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

## Search Tips

| Query | What it finds |
|-------|--------------|
| `project deadline` | paragraphs containing both words anywhere |
| `"project deadline"` | that exact phrase in that order |
| `meet*` | meeting, meets, meeting — prefix wildcard |
| `deadline NEAR/3 budget` | deadline and budget within 3 words of each other |

## Roadmap

- [ ] Tag/label documents for grouping
- [ ] Export search results
- [ ] Dark mode
- [ ] Drag-and-drop file upload
- [ ] Folder/collection support
