# Trading Env Sim launcher (Windows PowerShell).
#   .\start.ps1                 start the app and open the browser
#   .\start.ps1 --port 8080     any arguments are passed to "python -m sim serve"
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "Creating the Python virtual environment (.venv)..."
    if (Get-Command py -ErrorAction SilentlyContinue) { py -3 -m venv .venv } else { python -m venv .venv }
    if (-not (Test-Path $python)) { throw "Python 3.10 or newer is required: https://www.python.org/downloads/" }
}
& $python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) { throw "Python 3.10 or newer is required (found an older version in .venv)." }

# (Re)install dependencies the first time and whenever pyproject.toml changes
$stamp = Join-Path $PSScriptRoot ".venv\.installed"
if (-not (Test-Path $stamp) -or (Get-Item pyproject.toml).LastWriteTime -gt (Get-Item $stamp).LastWriteTime) {
    Write-Host "Installing Python packages..."
    & $python -m pip install --disable-pip-version-check -q --upgrade pip
    & $python -m pip install --disable-pip-version-check -q -e ".[dev]"
    if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
    New-Item -ItemType File -Force $stamp | Out-Null
}

# The web UI ships pre-built in frontend\dist. Rebuild only if it is missing and Node.js is available.
if (-not (Test-Path "frontend\dist\index.html")) {
    if (Get-Command npm -ErrorAction SilentlyContinue) {
        Push-Location frontend
        npm install
        npm run build
        Pop-Location
    } else {
        Write-Warning "frontend\dist is missing and Node.js is not installed: the API will run without the web UI."
    }
}

& $python -m sim serve @args
