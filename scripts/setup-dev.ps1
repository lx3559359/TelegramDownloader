$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$buildTemp = Join-Path $projectRoot '.build-temp'
$pipCache = Join-Path $projectRoot '.tool-cache\pip'
$userProfile = Join-Path $projectRoot '.tool-cache\user-profile'
$roaming = Join-Path $userProfile 'Roaming'
$local = Join-Path $userProfile 'Local'

New-Item -ItemType Directory -Force -Path $buildTemp, $pipCache, $roaming, $local | Out-Null
$env:TEMP = $buildTemp
$env:TMP = $buildTemp
$env:PIP_CACHE_DIR = $pipCache
$env:APPDATA = $roaming
$env:LOCALAPPDATA = $local

$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    py -3.12 -m venv (Join-Path $projectRoot '.venv')
}

& $venvPython -m pip install --disable-pip-version-check -r (
    Join-Path $projectRoot 'requirements-dev.txt'
)

