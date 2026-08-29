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
$existingAppDir = Assert-ProjectChild (Join-Path $dist 'TelegramDownloader')
$helperOutput = Assert-ProjectChild (Join-Path $dist 'UpdateHelper.exe')
$ownedBuildOutputs = @(
    $work,
    $existingAppDir,
    $helperOutput
)
$preservationRoot = Assert-ProjectChild (Join-Path $buildTemp (
    'build-runtime-preservation-' + [Guid]::NewGuid().ToString('N')
))
$preservedHashes = @{}
$preservedFiles = @(
    'data\database\catalog.sqlite3',
    'data\cache\thumbnails\preserve.thumb',
    'data\sentinel.keep'
)

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

$existingExe = Assert-ProjectChild (Join-Path $existingAppDir 'TelegramDownloader.exe')
if (Test-Path -LiteralPath $existingExe -PathType Leaf) {
    $running = Get-CimInstance Win32_Process | Where-Object {
        $_.ExecutablePath -and
        ([IO.Path]::GetFullPath($_.ExecutablePath) -eq $existingExe)
    }
    if ($running) {
        throw 'Close the project-local TelegramDownloader before rebuilding it.'
    }
}

$preservedRuntime = $false
foreach ($relativePath in $preservedFiles) {
    $candidate = Assert-ProjectChild (Join-Path $existingAppDir $relativePath)
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
        $preservedHashes[$relativePath] = (Get-FileHash -Algorithm SHA256 -LiteralPath $candidate).Hash
    }
}
foreach ($runtimeData in ('data', 'downloads')) {
    $source = Assert-ProjectChild (Join-Path $existingAppDir $runtimeData)
    if (Test-Path -LiteralPath $source) {
        New-Item -ItemType Directory -Force -Path $preservationRoot | Out-Null
        $preserved = Assert-ProjectChild (Join-Path $preservationRoot $runtimeData)
        Copy-Item -LiteralPath $source -Destination $preserved -Recurse -Force
        $preservedRuntime = $true
    }
}

try {
    foreach ($ownedOutput in $ownedBuildOutputs) {
        if (Test-Path -LiteralPath $ownedOutput) {
            Remove-Item -LiteralPath $ownedOutput -Recurse -Force
        }
    }
    New-Item -ItemType Directory -Force -Path $work, $dist | Out-Null

    $python = Join-Path $projectRoot '.venv\Scripts\python.exe'
    $pythonBasePrefix = (& $python -c "import sys; print(sys.base_prefix)").Trim()
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $trustedBuildPath = @(
        (Split-Path -Parent $python)
        $pythonBasePrefix
        (Join-Path $pythonBasePrefix 'DLLs')
        [Environment]::GetFolderPath([Environment+SpecialFolder]::System)
        [Environment]::GetFolderPath([Environment+SpecialFolder]::Windows)
    ) | Where-Object {
        $_ -and (Test-Path -LiteralPath $_ -PathType Container)
    } | Select-Object -Unique
    $inheritedPath = $env:PATH
    $pyinstallerExitCode = 0
    try {
        $env:PATH = $trustedBuildPath -join [IO.Path]::PathSeparator
        & $python -m PyInstaller --noconfirm --clean --workpath $work --distpath $dist (Join-Path $projectRoot 'TelegramDownloader.spec')
        $pyinstallerExitCode = $LASTEXITCODE
    } finally {
        $env:PATH = $inheritedPath
    }
    if ($pyinstallerExitCode -ne 0) { exit $pyinstallerExitCode }

    $appDir = Assert-ProjectChild (Join-Path $dist 'TelegramDownloader')
    $helperExe = $helperOutput
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
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $privateArchive = $null
    try {
        $privateArchive = [System.IO.Compression.ZipFile]::OpenRead($zip)
        foreach ($archiveEntry in $privateArchive.Entries) {
            $normalizedEntry = $archiveEntry.FullName.Replace('\', '/').Trim('/').ToLowerInvariant()
            $entryName = [IO.Path]::GetFileName($normalizedEntry)
            $privateRoot = (
                $normalizedEntry -eq 'data' -or
                $normalizedEntry.StartsWith('data/') -or
                $normalizedEntry -eq 'downloads' -or
                $normalizedEntry.StartsWith('downloads/')
            )
            $privateName = (
                $entryName -eq 'storage-state.json' -or
                $entryName -eq 'app.log' -or
                $entryName -eq 'tasks.sqlite3' -or
                $entryName -eq 'catalog.sqlite3' -or
                $entryName -eq 'secrets.dat' -or
                $entryName.EndsWith('.part') -or
                $entryName.Contains('.corrupt')
            )
            if ($privateRoot -or $privateName) {
                throw 'Portable ZIP contains private runtime entry'
            }
        }
    } finally {
        if ($null -ne $privateArchive) {
            $privateArchive.Dispose()
        }
    }
    $zipValidation = Assert-ProjectChild (Join-Path $buildTemp 'portable-zip-validation')
    if (Test-Path -LiteralPath $zipValidation) {
        Remove-Item -LiteralPath $zipValidation -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $zipValidation | Out-Null
    try {
        Expand-Archive -LiteralPath $zip -DestinationPath $zipValidation -Force
        foreach ($runtimeData in ('data', 'downloads')) {
            $unexpected = Assert-ProjectChild (Join-Path $zipValidation $runtimeData)
            if (Test-Path -LiteralPath $unexpected) {
                throw "Portable ZIP unexpectedly contains user data: $runtimeData"
            }
        }
    } finally {
        if (Test-Path -LiteralPath $zipValidation) {
            Remove-Item -LiteralPath $zipValidation -Recurse -Force
        }
    }
    Write-Output $zip
} finally {
    if ($preservedRuntime) {
        New-Item -ItemType Directory -Force -Path $existingAppDir | Out-Null
        foreach ($runtimeData in ('data', 'downloads')) {
            $preserved = Assert-ProjectChild (Join-Path $preservationRoot $runtimeData)
            if (-not (Test-Path -LiteralPath $preserved)) {
                continue
            }
            $destination = Assert-ProjectChild (Join-Path $existingAppDir $runtimeData)
            if (Test-Path -LiteralPath $destination) {
                Remove-Item -LiteralPath $destination -Recurse -Force
            }
            Copy-Item -LiteralPath $preserved -Destination $destination -Recurse -Force
        }
        foreach ($relativePath in $preservedHashes.Keys) {
            $restored = Assert-ProjectChild (Join-Path $existingAppDir $relativePath)
            if (-not (Test-Path -LiteralPath $restored -PathType Leaf)) {
                throw "Build did not restore preserved user data: $relativePath"
            }
            $restoredHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $restored).Hash
            if ($restoredHash -ne $preservedHashes[$relativePath]) {
                throw "Build changed preserved user data: $relativePath"
            }
        }
        Remove-Item -LiteralPath $preservationRoot -Recurse -Force
    }
}
