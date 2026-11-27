# PyInstaller spec for the portable Windows build. Run packaging\build-windows.ps1 rather than this directly.
#
# One analysis, two executables in one folder:
#   Meniscus.exe         windowed: the app (server + app window)
#   meniscus-engine.exe  console:  runs the bundled gallery-dl / yt-dlp for the server
# The server starts the engine with CREATE_NO_WINDOW, so no console window appears.
import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.path.dirname(SPECPATH)
ICON = os.path.join(SPECPATH, "meniscus.ico")

datas = [(os.path.join(ROOT, "web"), "web"), (os.path.join(ROOT, "config.example.json"), ".")]
binaries = []
hiddenimports = ["server", "encode", "winplat"]
# gallery-dl loads its extractors by name at runtime, so PyInstaller cannot see them
hiddenimports += collect_submodules("gallery_dl")
for pkg in ("curl_cffi", "certifi"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    [os.path.join(ROOT, "meniscus_win.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "secretstorage", "jeepney", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

app = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Meniscus", icon=ICON,
          console=False, upx=False)
engine = EXE(pyz, a.scripts, [], exclude_binaries=True, name="meniscus-engine", icon=ICON,
             console=True, upx=False)

COLLECT(app, engine, a.binaries, a.datas, name="Meniscus", upx=False)
