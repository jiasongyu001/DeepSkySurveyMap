# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for DeepSkySurveyMap standalone build."""

import os
PROJECT = os.path.abspath('.')

a = Analysis(
    ['main.py'],
    pathex=[PROJECT],
    binaries=[],
    datas=[
        ('stars.csv', '.'),
        ('constellations.py', '.'),
    ],
    hiddenimports=[
        'astropy.wcs',
        'astropy.io.fits',
        'numpy',
        'PIL',
        'requests',
        'urllib3',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['matplotlib', 'tkinter', 'scipy'],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='DeepSkySurveyMap',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,          # No console window — GUI app
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='DeepSkySurveyMap',
)
