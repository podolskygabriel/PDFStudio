# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for PDF Studio.
Build with: pyinstaller pdf_studio.spec
"""

import sys
from pathlib import Path

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'PIL',
        'PIL.Image',
        'fitz',
        'PyQt6',
        'PyQt6.QtWidgets',
        'PyQt6.QtGui',
        'PyQt6.QtCore',
        'PyQt6.sip',
        'cryptography',
        'cryptography.hazmat.primitives.serialization.pkcs12',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy.testing'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='PDFStudio',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # No console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,  # Add icon path here: icon='assets/icon.ico'
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='PDFStudio',
)
