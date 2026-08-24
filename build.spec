"""Build NTERO as one Windows executable."""

import shutil
from pathlib import Path

from PyInstaller.building.api import EXE
from PyInstaller.building.api import PYZ
from PyInstaller.building.build_main import Analysis
from PyInstaller.utils.hooks import collect_submodules

project_root = Path.cwd()
package_root = project_root / "src" / "ntero"
native_source = project_root / "target" / "release" / "_native.dll"
native_module = project_root / "build" / "native" / "_native.pyd"
native_module.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(native_source, native_module)


def package_data(source: str, destination: str = "") -> tuple[str, str]:
    """Map a project file into the frozen ntero package."""
    package_destination = Path("ntero") / destination
    return str(project_root / source), package_destination.as_posix()


analysis = Analysis(
    [str(package_root / "__main__.py")],
    pathex=[str(project_root / "src")],
    binaries=[(str(native_module), "ntero")],
    datas=[
        package_data("pyproject.toml"),
        package_data(
            "ThirdPartyLicenses/squish-LICENSE.txt",
            "ThirdPartyLicenses",
        ),
    ],
    hiddenimports=["ntero._native", *collect_submodules("swingset.backends")],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
python_archive = PYZ(analysis.pure)

executable = EXE(
    python_archive,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="ntero",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
