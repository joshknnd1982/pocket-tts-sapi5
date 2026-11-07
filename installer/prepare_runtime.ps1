# Assembles the self-contained Python runtime that the installer bundles,
# so end users need no Python installation.
#
#   runtime\PocketTTSHost.exe        (renamed pythonw.exe, kill-able by name)
#   runtime\Lib\site-packages\...    (pocket-tts + torch CPU + soundfile)
#
# Run from the repository root:  powershell -ExecutionPolicy Bypass -File installer\prepare_runtime.ps1

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$runtime = Join-Path $root "runtime"
$pyVersion = "3.10.11"
$embedUrl = "https://www.python.org/ftp/python/$pyVersion/python-$pyVersion-embed-amd64.zip"
$devPython = "$env:USERPROFILE\.pockettts\venv\Scripts\python.exe"
if (-not (Test-Path $devPython)) { $devPython = "py" }

# The pocket-tts the runtime is built from: Kyutai's 4.0.1 with their fixes
# up to 2026-11-06 (kyutai-labs/pocket-tts main). The host relies on its
# internals, so a different version is a deliberate change, tested first.
$pocketTtsCommit = "28770c06f7deb1b65617834e60a6add7a4ff5b6d"
$pocketTts = Join-Path $root "bin\pocket-tts"
if (-not (Test-Path $pocketTts)) {
    git clone --quiet https://github.com/kyutai-labs/pocket-tts.git $pocketTts
    if ($LASTEXITCODE -ne 0) { throw "could not clone pocket-tts" }
}
git -C $pocketTts fetch --quiet origin
git -C $pocketTts checkout --quiet $pocketTtsCommit
if ($LASTEXITCODE -ne 0) { throw "could not check out pocket-tts $pocketTtsCommit" }

if (Test-Path $runtime) {
    Write-Host "Removing existing runtime directory..."
    Remove-Item -Recurse -Force $runtime
}
New-Item -ItemType Directory -Force $runtime | Out-Null

Write-Host "Downloading Python $pyVersion embeddable package..."
$zip = Join-Path $env:TEMP "python-embed-amd64.zip"
Invoke-WebRequest -Uri $embedUrl -OutFile $zip
Expand-Archive -Path $zip -DestinationPath $runtime -Force
Remove-Item $zip

# Let the embedded interpreter see Lib\site-packages.
$pth = Join-Path $runtime "python310._pth"
Set-Content -Path $pth -Encoding ascii -Value @(
    "python310.zip",
    ".",
    "Lib\site-packages",
    "import site"
)

Write-Host "Installing pocket-tts and dependencies into the runtime..."
$site = Join-Path $runtime "Lib\site-packages"
New-Item -ItemType Directory -Force $site | Out-Null
& $devPython -m pip install --target $site --no-warn-script-location `
    (Join-Path $root "bin\pocket-tts") soundfile
if ($LASTEXITCODE -ne 0) { throw "pip install into runtime failed" }

Write-Host "Trimming unneeded files..."
# Conservative: torch lazily imports torchgen, functorch and testing bits
# from inference code paths, so only clearly inert payload is removed.
$trim = @(
    "torch\include", "torch\test", "torch\utils\benchmark",
    "bin", "share"
)
foreach ($d in $trim) {
    $p = Join-Path $site $d
    if (Test-Path $p) { Remove-Item -Recurse -Force $p }
}
Get-ChildItem -Path $site -Recurse -Directory -Filter "__pycache__" |
    Remove-Item -Recurse -Force
Get-ChildItem -Path (Join-Path $site "torch\lib") -Filter "*.lib" -ErrorAction SilentlyContinue |
    Remove-Item -Force

# The host process runs under its own executable name so the uninstaller
# (and users) can identify and stop it without touching other Python apps.
Copy-Item (Join-Path $runtime "pythonw.exe") (Join-Path $runtime "PocketTTSHost.exe")

$size = [math]::Round((Get-ChildItem -Recurse $runtime | Measure-Object Length -Sum).Sum / 1MB)
Write-Host "Runtime ready at $runtime ($size MB)"
