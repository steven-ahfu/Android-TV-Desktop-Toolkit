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

$venvPython = "$root\.venv\Scripts\python.exe"
$venvPyInstaller = "$root\.venv\Scripts\pyinstaller.exe"

# Resolve the Tcl/Tk runtime directory from the base Python (needed for Tkinter
# apps bundled with PyInstaller — the venv doesn't copy these files, so we pull
# them from the base Python identified in pyvenv.cfg).
$tclDir = & $venvPython -c "import sys,os; p=os.path.join(sys.prefix,'tcl'); print(p if os.path.isdir(p) else '')"

$extraArgs = @(
    "--noconfirm",
    "--onefile",
    "--windowed",
    "--name", "Android TV Desktop Toolkit",
    "--icon", "$root\assets\icon.ico",
    "--add-data", "$root\assets;assets",
    "--add-data", "$(& $venvPython -c 'import customtkinter, os; print(os.path.dirname(customtkinter.__file__))');customtkinter"
)

if ($tclDir) {
    $extraArgs += "--add-data"
    $extraArgs += "$tclDir;tcl"
}

& $venvPyInstaller @extraArgs "$root\android_tv_tools.py"

Write-Output ""
Write-Output "Build complete. Output: dist\Android TV Desktop Toolkit.exe"

# Create desktop shortcut pointing to the built exe
$exe = "$root\dist\Android TV Desktop Toolkit.exe"
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut("$env:USERPROFILE\Desktop\Android TV Desktop Toolkit.lnk")
$sc.TargetPath       = $exe
$sc.WorkingDirectory = "$root\dist"
$sc.Description      = "Android TV Desktop Toolkit"
$sc.Save()
Write-Output "Shortcut created -> Desktop."
