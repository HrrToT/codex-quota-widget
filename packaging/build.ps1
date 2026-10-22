param(
    [string]$Python = "python",
    [string]$Iscc = "ISCC.exe"
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
Push-Location $projectRoot
try {
    & $Python -m PyInstaller --noconfirm --clean --windowed --onedir --name CodexQuotaWidget --icon assets/app.ico --add-data "assets;assets" --collect-all PIL quota_widget.py
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed' }
    & $Python packaging/collect_licenses.py
    if ($LASTEXITCODE -ne 0) { throw "License collection failed" }
    Copy-Item LICENSE, README.md, THIRD_PARTY_NOTICES.md dist/CodexQuotaWidget/
    Copy-Item docs dist/CodexQuotaWidget/ -Recurse -Force
    New-Item -ItemType Directory dist/CodexQuotaWidget/assets -Force | Out-Null
    Copy-Item assets/SOURCE.md dist/CodexQuotaWidget/assets/
    & $Iscc /Qp packaging/installer.iss
    if ($LASTEXITCODE -ne 0) { throw 'Installer build failed' }
    Compress-Archive -Path dist/CodexQuotaWidget -DestinationPath release/CodexQuotaWidget-1.0.0-windows-x64-portable.zip -Force
    Get-ChildItem release -File | Where-Object Extension -in '.exe','.zip' | ForEach-Object {
        $hash = (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        "$hash  $($_.Name)"
    } | Set-Content release/SHA256SUMS.txt -Encoding ascii
} finally { Pop-Location }
