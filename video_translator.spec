# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs, copy_metadata

datas = []
binaries = []
hiddenimports = []

for pkg in [
    "TTS",
    "faster_whisper",
    "ctranslate2",
    "transformers",
    "tokenizers",
    "webview",
    "pysrt",
]:
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# torch/torchaudio: only pull their binary extensions (.pyd/.dll) and version
# metadata (many libraries check importlib.metadata.version("torch")).
# collect_all() on these two floods the build with thousands of bogus
# "Hidden import ... not found" lines for internal submodules (torch.distributed.*,
# torch.testing.*, etc.) that this project never imports and that PyInstaller
# cannot always resolve cleanly across torch versions. Those lines are harmless
# noise, but skipping collect_all() here avoids them and speeds up the build.
for pkg in ["torch", "torchaudio"]:
    binaries += collect_dynamic_libs(pkg)
    datas += copy_metadata(pkg)
hiddenimports += ["torch", "torchaudio"]

datas += [("app/web", "app/web")]

a = Analysis(
    ["desktop_app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="VideoTranslator",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="VideoTranslator",
)
