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

$arguments = @(
    "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
    "--windowed", "--name", "LegadoReader",
    "--workpath", "build/windows-packaging", "--distpath", "dist",
    "--add-data", "resource;resource",
    "--add-data", "app/resources;app/resources"
) + $binaryArgs + @("main.py")

& $python @arguments

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed with exit code $LASTEXITCODE"
}

& ".\dist\LegadoReader.exe" --smoke-test
if ($LASTEXITCODE -ne 0) {
    throw "Frozen executable smoke test failed with exit code $LASTEXITCODE"
}

Write-Host "Windows executable verified: dist\LegadoReader.exe"
