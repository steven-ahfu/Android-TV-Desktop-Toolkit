# Build Android TV Desktop Toolkit as a standalone Windows exe via PyInstaller.
# Run from the repo root: .\scripts\build.ps1
#
# Requirements:
#   uv (https://docs.astral.sh/uv/)
#   Python 3.12 or 3.13 recommended (3.14 not yet fully supported by PyInstaller)

$root = Split-Path $PSScriptRoot -Parent

Set-Location $root

Write-Output "Syncing dependencies..."
uv sync
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$venvPython      = "$root\.venv\Scripts\python.exe"
$venvPyInstaller = "$root\.venv\Scripts\pyinstaller.exe"
$specFile        = "$root\Android TV Desktop Toolkit.spec"

# Generate the spec file (handles Tcl/Tk data from base Python)
Write-Output "Generating spec file..."
& $venvPython "$root\scripts\generate_spec.py" "$root"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

# Build from the spec file.
# NOTE: output goes to dist_clean\ (canonical output dir — the desktop shortcut
# points here; the old dist\ and dist_new\ held stale/broken builds and were removed).
$distDir = "$root\dist_clean"
Write-Output "Building..."
& $venvPyInstaller --noconfirm --clean --distpath "$distDir" "$specFile"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Output ""
Write-Output "Build complete. Output: $distDir\Android TV Desktop Toolkit.exe"

# Create desktop shortcut pointing to the built exe
$exe = "$distDir\Android TV Desktop Toolkit.exe"
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut("$env:USERPROFILE\Desktop\Android TV Desktop Toolkit.lnk")
$sc.TargetPath       = $exe
$sc.WorkingDirectory = "$distDir"
$sc.Description      = "Android TV Desktop Toolkit"
$sc.Save()
Write-Output "Shortcut created -> Desktop."
