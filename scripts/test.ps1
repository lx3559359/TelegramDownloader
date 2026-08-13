$ErrorActionPreference = 'Stop'

$projectRoot = (Resolve-Path (Split-Path -Parent $PSScriptRoot)).Path
$buildTemp = Join-Path $projectRoot '.build-temp'
$toolCache = Join-Path $projectRoot '.tool-cache'
$testProfile = Join-Path $buildTemp 'test-user-profile'
$roaming = Join-Path $testProfile 'Roaming'
$local = Join-Path $testProfile 'Local'
$pythonCache = Join-Path $toolCache 'pycache'
New-Item -ItemType Directory -Force -Path $buildTemp, $toolCache, $roaming, $local, $pythonCache | Out-Null

$env:TEMP = $buildTemp
$env:TMP = $buildTemp
$env:APPDATA = $roaming
$env:LOCALAPPDATA = $local
$env:PIP_CACHE_DIR = Join-Path $toolCache 'pip'
$env:PYINSTALLER_CONFIG_DIR = Join-Path $toolCache 'pyinstaller'
$env:PYTHONPYCACHEPREFIX = $pythonCache
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:QT_QPA_PLATFORM = 'offscreen'

$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$ruff = Join-Path $projectRoot '.venv\Scripts\ruff.exe'
& $python -m pytest -q
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $ruff check src tests
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
