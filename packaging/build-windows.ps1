# Builds the portable Windows version of Meniscus into dist\Meniscus and zips it.
#
#   powershell -ExecutionPolicy Bypass -File packaging\build-windows.ps1
#   powershell -ExecutionPolicy Bypass -File packaging\build-windows.ps1 -WithFfmpeg
#
# -WithFfmpeg copies ffmpeg.exe and ffprobe.exe from your PATH into the folder, so the build also
# makes thumbnails and Discord copies on machines without ffmpeg. Without it, Meniscus uses
# whatever ffmpeg is on the PATH of the machine it runs on.
param([switch]$WithFfmpeg)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path ".venv\Scripts\python.exe")) { python -m venv .venv }
& .venv\Scripts\python.exe -m pip install -q -r requirements.txt pyinstaller
if ($LASTEXITCODE) { throw "pip install failed" }

$out = "dist\Meniscus"
$running = Get-Process Meniscus, meniscus-engine -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path.StartsWith((Resolve-Path -LiteralPath "." ).Path + "\dist\") }
if ($running) { throw "Meniscus is running from $out. Close its window, wait for downloads to finish, then build again." }

# PyInstaller replaces dist\Meniscus wholesale, so keep the data folder (library, settings, logins)
# of a copy that has been used out of the way while it builds.
$kept = "build\data-kept"
if (Test-Path "$out\data") {
    if (Test-Path $kept) { throw "$kept exists from an earlier interrupted build. Move it back to $out\data first." }
    New-Item -ItemType Directory -Force build | Out-Null
    Move-Item "$out\data" $kept
}

# PyInstaller resolves DLLs such as libssl through PATH. Put the base Python's own DLL folders first
# and drop Git for Windows' mingw64, whose OpenSSL doesn't match Python's _ssl module.
$base = (& .venv\Scripts\python.exe -c "import sys; print(sys.base_prefix)").Trim()
$keep = $env:PATH -split ";" | Where-Object { $_ -and $_ -notmatch "\\Git\\(mingw64|usr)\\" }
$env:PATH = (@($base, "$base\DLLs", "$base\Library\bin") + $keep) -join ";"

try {
    & .venv\Scripts\pyinstaller.exe --noconfirm --clean --distpath dist --workpath build packaging\meniscus.spec
    if ($LASTEXITCODE) { throw "PyInstaller failed" }
} finally {
    if (Test-Path $kept) { New-Item -ItemType Directory -Force $out | Out-Null; Move-Item $kept "$out\data" }
}

Copy-Item README.md, LICENSE $out
Copy-Item packaging\README-windows.txt "$out\README-windows.txt"
if ($WithFfmpeg) {
    foreach ($tool in "ffmpeg", "ffprobe") {
        $exe = (Get-Command $tool -ErrorAction Stop).Source
        Copy-Item $exe $out
    }
}

$zip = "dist\Meniscus-windows-portable.zip"
if (Test-Path $zip) { Remove-Item $zip }
# Zip everything except the data folder, which holds this machine's library and logins.
$stage = "build\zip-stage\Meniscus"
if (Test-Path "build\zip-stage") { Remove-Item -Recurse -Force "build\zip-stage" }
New-Item -ItemType Directory -Force $stage | Out-Null
Get-ChildItem $out | Where-Object Name -ne "data" | Copy-Item -Destination $stage -Recurse
Compress-Archive -Path $stage -DestinationPath $zip
Remove-Item -Recurse -Force "build\zip-stage"
"Built $out and $zip"
