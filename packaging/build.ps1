# Requer: pip install -r requirements.txt pyinstaller pillow  +  Inno Setup 6
# Gera build\dist\NoPingeyson\NoPingeyson.exe e o instalador em build\installer.
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

$version = (python -c "import nopingeyson; print(nopingeyson.__version__)").Trim()
Write-Host "NoPingeyson $version"

python packaging\make_icon.py

python -m PyInstaller --noconfirm --clean `
  --name NoPingeyson --windowed --uac-admin `
  --icon "$root\packaging\nopingeyson.ico" `
  --add-data "$root\nopingeyson\ui;nopingeyson\ui" `
  --collect-all pydivert --collect-all webview `
  --distpath build\dist --workpath build\work --specpath build `
  packaging\launcher.py
if ($LASTEXITCODE) { throw "PyInstaller falhou" }

$iscc = @(
  "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
  "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
  "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 não encontrado (winget install JRSoftware.InnoSetup)" }
& $iscc "/DAppVersion=$version" packaging\installer.iss
if ($LASTEXITCODE) { throw "Inno Setup falhou" }

Get-ChildItem build\installer\*.exe | ForEach-Object { Write-Host "Instalador: $($_.FullName)" }
