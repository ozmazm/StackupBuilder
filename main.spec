# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path


ROOT = Path.cwd()
CONSOLE_ENABLED = os.environ.get("STACKUP_EDITOR_CONSOLE", "1").strip().lower() not in {"0", "false", "no"}


def collect_tree(source_root: Path, destination_root: str) -> list[tuple[str, str]]:
    if not source_root.exists():
        return []
    collected: list[tuple[str, str]] = []
    for path in source_root.rglob("*"):
        if path.is_file():
            relative_parent = path.relative_to(source_root).parent
            destination = Path(destination_root) / relative_parent
            collected.append((str(path), str(destination)))
    return collected


def collect_optional_file(source_path: Path, destination_root: str) -> list[tuple[str, str]]:
    if not source_path.exists() or not source_path.is_file():
        return []
    return [(str(source_path), destination_root)]


datas = [
    (str(ROOT / "tools" / "field_solver_runner.mjs"), "tools"),
]
datas += collect_optional_file(ROOT / "TransmissionLineTemp.xlsx", ".")
datas += collect_optional_file(ROOT / "stackup_editor" / "flex_core_material_catalog.json", "stackup_editor")
datas += collect_optional_file(ROOT / "stackup_editor" / "coverlay_material_catalog.json", "stackup_editor")
datas += collect_tree(ROOT / "data", "data")
datas += collect_tree(ROOT / "stackup_editor" / "ui", "stackup_editor/ui")
datas += collect_tree(ROOT / "js_2d_fields-master" / "src", "js_2d_fields-master/src")
datas += collect_tree(ROOT / "runtime", "runtime")


a = Analysis(
    ["main.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "PySide6.QtUiTools",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="StackUp Editor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=CONSOLE_ENABLED,
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
    upx=False,
    upx_exclude=[],
    name="StackUp Editor",
)
