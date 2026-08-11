$ErrorActionPreference = "Stop"

$python = "D:\env_manger\minicaoda\envs\legodo_booksource_check\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    $python = "python"
}
$condaBin = Split-Path -Parent $python
$condaBin = Join-Path $condaBin "Library\bin"
$runtimeDlls = @(
    "libcrypto-3-x64.dll", "libssl-3-x64.dll", "liblzma.dll",
    "libbz2.dll", "libexpat.dll", "ffi.dll", "sqlite3.dll"
)
$binaryArgs = @()
foreach ($dll in $runtimeDlls) {
    $path = Join-Path $condaBin $dll
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Required Conda runtime DLL is missing: $path"
    }
    $binaryArgs += @("--add-binary", "$path;.")
}

$flet = Join-Path (Split-Path -Parent $python) "Scripts\flet.exe"
if (-not (Test-Path -LiteralPath $flet)) {
    throw "Flet CLI is missing: $flet"
}

$arguments = @(
    "pack", "main.py", "--yes", "--name", "LegadoReader",
    "--distpath", "dist",
    "--add-data", "resource;resource", "app/resources;app/resources"
) + $binaryArgs

& $flet @arguments

if ($LASTEXITCODE -ne 0) {
    throw "Flet pack failed with exit code $LASTEXITCODE"
}

& ".\dist\LegadoReader.exe" --smoke-test
if ($LASTEXITCODE -ne 0) {
    throw "Frozen executable smoke test failed with exit code $LASTEXITCODE"
}

Write-Host "Windows executable verified: dist\LegadoReader.exe"
