# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['cms_exe.py'],
    pathex=[],
    binaries=[],
    datas=[('cms/ui_assets', 'cms/ui_assets'), ('skills', 'cms/builtin_skills')],
    hiddenimports=['anthropic'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['torch', 'torchvision', 'torchaudio', 'numpy', 'scipy', 'pandas', 'matplotlib', 'cv2', 'PIL', 'lxml', 'IPython', 'jupyter', 'pytest', 'coverage', 'rich', 'pygments', 'tkinter', 'setuptools', 'PyInstaller'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='CMS',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='CMS',
)
