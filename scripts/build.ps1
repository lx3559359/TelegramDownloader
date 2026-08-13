$ErrorActionPreference = 'Stop'

$projectRoot = (Resolve-Path (Split-Path -Parent $PSScriptRoot)).Path
$projectPrefix = $projectRoot.TrimEnd('\') + '\'

function Assert-ProjectChild([string]$Candidate) {
    $resolved = [IO.Path]::GetFullPath($Candidate)
    if (-not $resolved.StartsWith($projectPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Build path escaped project: $resolved"
    }
    return $resolved
}

$buildTemp = Assert-ProjectChild (Join-Path $projectRoot '.build-temp')
$toolCache = Assert-ProjectChild (Join-Path $projectRoot '.tool-cache')
$work = Assert-ProjectChild (Join-Path $projectRoot 'build')
$dist = Assert-ProjectChild (Join-Path $projectRoot 'dist')
$buildProfile = Assert-ProjectChild (Join-Path $buildTemp 'build-user-profile')

$env:TEMP = $buildTemp
$env:TMP = $buildTemp
$env:APPDATA = Join-Path $buildProfile 'Roaming'
$env:LOCALAPPDATA = Join-Path $buildProfile 'Local'
$env:PIP_CACHE_DIR = Join-Path $toolCache 'pip'
$env:PYINSTALLER_CONFIG_DIR = Join-Path $toolCache 'pyinstaller'
$env:PYTHONPYCACHEPREFIX = Join-Path $toolCache 'pycache'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:QT_QPA_PLATFORM = 'offscreen'
New-Item -ItemType Directory -Force -Path $buildTemp, $toolCache, $env:APPDATA, $env:LOCALAPPDATA | Out-Null

& (Join-Path $PSScriptRoot 'setup-dev.ps1')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& (Join-Path $PSScriptRoot 'test.ps1')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

foreach ($directory in ($work, $dist)) {
    if (Test-Path -LiteralPath $directory) {
        Remove-Item -LiteralPath $directory -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
}

$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
& $python -m PyInstaller --noconfirm --clean --workpath $work --distpath $dist (Join-Path $projectRoot 'TelegramDownloader.spec')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$appDir = Assert-ProjectChild (Join-Path $dist 'TelegramDownloader')
$helperExe = Assert-ProjectChild (Join-Path $dist 'UpdateHelper.exe')
if (-not (Test-Path -LiteralPath $helperExe -PathType Leaf)) {
    throw "Packaged update helper missing: $helperExe"
}
Copy-Item -LiteralPath $helperExe -Destination (Join-Path $appDir 'UpdateHelper.exe') -Force

$version = & $python -c "from telegram_downloader import __version__; print(__version__)"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $python (Join-Path $PSScriptRoot 'generate_runtime_inventory.py') --root $appDir --version $version
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& (Join-Path $PSScriptRoot 'smoke.ps1')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

foreach ($runtimeData in ('data', 'downloads')) {
    $candidate = Assert-ProjectChild (Join-Path $appDir $runtimeData)
    if (Test-Path -LiteralPath $candidate) {
        Remove-Item -LiteralPath $candidate -Recurse -Force
    }
}

$zip = Assert-ProjectChild (Join-Path $dist "TelegramDownloader-$version-win-x64-portable.zip")
if (Test-Path -LiteralPath $zip) {
    Remove-Item -LiteralPath $zip -Force
}
Compress-Archive -Path (Join-Path $appDir '*') -DestinationPath $zip -CompressionLevel Optimal
Write-Output $zip
