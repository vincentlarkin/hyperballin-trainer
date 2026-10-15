# Build from the repository root with: python3 -m PyInstaller hyperballin-trainer.spec
a = Analysis(
    ['trainer.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['gi.repository.Gio', 'gi.repository.GLib'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='hyperballin-trainer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    runtime_tmpdir=None,
)
