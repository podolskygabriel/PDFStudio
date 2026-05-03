# PDF Studio

**Free, open-source, local-first PDF editor.** View, annotate, edit text, fill forms, sign, OCR scanned documents, and send for signature — no subscription, no cloud, no Adobe.

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.10%2B-green.svg)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)

---

## Why PDF Studio?

Every serious document that needs to be signed and finalized is a PDF. In 2026, editing one shouldn't cost $23/month. PDF Studio gives you the 80% of Acrobat you actually use — completely free, running on your machine.

---

## Features

### View & Navigate
- Multi-page continuous scroll with page thumbnail sidebar
- Ctrl+scroll zoom, fit-to-width, keyboard page navigation
- Dark theme UI

### Annotate
- Highlight regions (drag to select)
- Add movable, editable text annotations
- Freehand ink drawing
- All overlays are draggable — right-click to resize or delete

### Edit Text In-Place
- Click existing text to modify it directly
- Auto font-size fitting within the original bounding box

### Fill & Sign Forms
- Auto-detects AcroForm fields (text, checkbox, combo box)
- Dedicated form panel for structured filling

### Sign Documents
- Draw signature with mouse (freehand canvas with guide line)
- Upload signature image (PNG with transparency)
- Click-to-place, drag to reposition, right-click to resize
- PKCS#12 (.pfx/.p12) cryptographic digital signature support

### OCR Scanned PDFs
- Tesseract integration — converts scanned pages to searchable text
- Invisible text layer overlay preserves original appearance
- Confidence reporting per page
- Auto-detects which pages need OCR vs. which already have text

### Send for Signature
- Email-based signing flow via your own SMTP (Gmail, Outlook, etc.)
- Professional HTML email template with signing instructions
- Local request tracker — view sent/pending/signed status
- All data stored locally in `~/.pdf_studio/`

### Save & Export
- Save / Save As with full annotation burn-in
- "Burn & Save" permanently writes overlays into the PDF
- Incremental save for quick overwrites

---

## Quick Start

### From Source (all platforms)

```bash
git clone https://github.com/podolskygabriel/PDFStudio.git
cd pdf-studio
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

### Standalone .exe (Windows)

Download the latest release from [Releases](https://github.com/podolskygabriel/PDFStudio/releases), unzip, and run `PDFStudio.exe`. No Python required.

### Build Your Own .exe

```bash
pip install pyinstaller
pyinstaller pdf_studio.spec --clean
# Output: dist/PDFStudio/PDFStudio.exe
```

Or use the included build script (Windows):
```
build.bat
```

---

## OCR Setup

OCR requires Tesseract installed separately:

| Platform | Install |
|----------|---------|
| Windows  | [UB-Mannheim installer](https://github.com/UB-Mannheim/tesseract/wiki) — add to PATH |
| macOS    | `brew install tesseract` |
| Linux    | `sudo apt install tesseract-ocr` |

PDF Studio auto-detects Tesseract. If it's installed and on PATH, the OCR button just works.

---

## Email Signing Setup

1. Go to **Settings → Email Setup**
2. Select your provider preset (Gmail, Outlook, Yahoo, iCloud) or enter custom SMTP
3. For Gmail: use an [App Password](https://myaccount.google.com/apppasswords), not your regular password
4. Click **Test Connection** to verify
5. Credentials are stored locally in `~/.pdf_studio/smtp_config.json`

---

## Keyboard Shortcuts

| Key             | Action              |
|-----------------|---------------------|
| `Ctrl+O`        | Open PDF            |
| `Ctrl+S`        | Save                |
| `Ctrl+Shift+S`  | Save As             |
| `V`             | Select tool         |
| `H`             | Highlight tool      |
| `T`             | Text annotation     |
| `D`             | Freehand draw       |
| `E`             | Edit existing text  |
| `S`             | Sign dialog         |
| `Ctrl+=`        | Zoom in             |
| `Ctrl+-`        | Zoom out            |
| `Ctrl+0`        | Fit to width        |
| `Ctrl+Scroll`   | Zoom in/out         |
| `Ctrl+Q`        | Exit                |

---

## Architecture

```
pdf-studio/
├── main.py              # App window, toolbar, menus, orchestration
├── pdf_engine.py         # PyMuPDF wrapper: render, text extract/edit, forms, save
├── canvas.py             # QGraphicsView canvas with annotation overlays
├── signature.py          # Signature draw/upload + PKI crypto signing
├── ocr.py                # Tesseract OCR integration
├── email_sign.py         # SMTP signing flow + request tracker
├── pdf_studio.spec       # PyInstaller build spec
├── build.bat             # Windows build script
├── pyproject.toml        # Python packaging config
├── requirements.txt      # pip dependencies
├── LICENSE               # MIT
├── CONTRIBUTING.md       # Contributor guidelines
└── .github/
    └── workflows/
        ├── ci.yml        # Lint + build on push/PR
        └── release.yml   # Auto-build releases on tag
```

---

## Roadmap

- [ ] Merge / split / reorder pages (drag-and-drop)
- [ ] Tabbed multi-document interface
- [ ] Redaction tool (compliant permanent redaction)
- [ ] Stamp library (Approved, Draft, Confidential)
- [ ] Bookmark / outline editing
- [ ] Ruler / measurement tools
- [ ] Homebrew formula + Flatpak packaging
- [ ] Windows MSI / NSIS installer

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). TL;DR: fork, branch, build something cool, PR it.

---

## License

MIT — do whatever you want with it.
