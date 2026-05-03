# Contributing to PDF Studio

Thanks for your interest in contributing! PDF Studio aims to be the free, open-source alternative to Adobe Acrobat that everyone deserves.

## Getting Started

1. Fork the repo and clone locally
2. Create a virtual environment:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # or .venv\Scripts\activate on Windows
   ```
3. Install dependencies:
   ```bash
   pip install -e ".[all,dev]"
   ```
4. Run the app:
   ```bash
   python main.py
   ```

## Development Guidelines

- **Python 3.10+** — we use modern syntax (match/case, union types with `|`)
- **PyQt6** — all UI code uses PyQt6, not PyQt5
- **Formatting** — run `ruff check .` before submitting
- **No external services** — PDF Studio runs 100% locally. The email flow uses the user's own SMTP

## What to Work On

Check the Issues tab for open tasks. Priority areas:

- **Merge / split / reorder pages** — drag-and-drop page management
- **Tabbed multi-document interface** — open multiple PDFs at once
- **Redaction tool** — permanent, compliant redaction
- **Bookmark / outline editing**
- **Accessibility** — keyboard navigation, screen reader support
- **Tests** — unit tests for `pdf_engine.py` and `ocr.py`
- **Packaging** — Homebrew formula, Flatpak, Windows installer (MSI/NSIS)

## Pull Request Process

1. Create a feature branch: `git checkout -b feature/your-feature`
2. Make your changes with clear commit messages
3. Test manually with a variety of PDFs (text-heavy, scanned, forms, signed)
4. Run the linter: `ruff check .`
5. Submit a PR with a description of what changed and why

## Architecture Overview

```
main.py          → App window, toolbar, menus, orchestration
pdf_engine.py    → PyMuPDF wrapper: render, text extract/edit, forms, save
canvas.py        → QGraphicsView canvas with annotation overlays
signature.py     → Signature draw/upload + PKI crypto signing
ocr.py           → Tesseract OCR integration
email_sign.py    → SMTP-based signing flow + request tracker
```

## Code of Conduct

Be respectful. Build cool stuff. Help others. That's it.
