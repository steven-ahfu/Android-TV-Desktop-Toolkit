# Build Android TV Tools as a standalone Windows exe via PyInstaller.
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

& $venvPyInstaller `
    --noconfirm `
    --onefile `
    --windowed `
    --name "Android TV Tools" `
    --icon "$root\assets\icon.ico" `
    --add-data "$root\assets;assets" `
    --add-data "$(& $venvPython -c 'import customtkinter, os; print(os.path.dirname(customtkinter.__file__))');customtkinter" `
    "$root\android_tv_tools.py"

Write-Output ""
Write-Output "Build complete. Output: dist\Android TV Tools.exe"

# Create desktop shortcut pointing to the built exe
$exe = "$root\dist\Android TV Tools.exe"
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut("$env:USERPROFILE\Desktop\Android TV Tools.lnk")
$sc.TargetPath       = $exe
$sc.WorkingDirectory = "$root\dist"
$sc.Description      = "Android TV Tools"
$sc.Save()
Write-Output "Shortcut created -> Desktop."
