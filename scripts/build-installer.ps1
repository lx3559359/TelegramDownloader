param(
    [switch]$SkipAppBuild
)

$ErrorActionPreference = 'Stop'

$projectRoot = (Resolve-Path (Split-Path -Parent $PSScriptRoot)).Path
$projectPrefix = $projectRoot.TrimEnd('\') + '\'

function Assert-ProjectChild([string]$Candidate) {
    $resolved = [IO.Path]::GetFullPath($Candidate)
    if (-not $resolved.StartsWith($projectPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Installer path escaped project: $resolved"
    }
    return $resolved
}

$buildTemp = Assert-ProjectChild (Join-Path $projectRoot '.build-temp\installer')
$toolCache = Assert-ProjectChild (Join-Path $projectRoot '.tool-cache')
$downloadCache = Assert-ProjectChild (Join-Path $toolCache 'downloads')
$compilerRoot = Assert-ProjectChild (Join-Path $toolCache 'inno-setup-7.0.2')
$compiler = Assert-ProjectChild (Join-Path $compilerRoot 'ISCC.exe')
$releaseDir = Assert-ProjectChild (Join-Path $projectRoot 'dist\release')
$appDir = Assert-ProjectChild (Join-Path $projectRoot 'dist\TelegramDownloader')
$buildProfile = Assert-ProjectChild (Join-Path $buildTemp 'user-profile')

$env:TEMP = Assert-ProjectChild (Join-Path $buildTemp 'temp')
$env:TMP = $env:TEMP
$env:APPDATA = Assert-ProjectChild (Join-Path $buildProfile 'Roaming')
$env:LOCALAPPDATA = Assert-ProjectChild (Join-Path $buildProfile 'Local')
New-Item -ItemType Directory -Force -Path $buildTemp, $toolCache, $downloadCache, $env:TEMP, $env:APPDATA, $env:LOCALAPPDATA | Out-Null

if (-not $SkipAppBuild) {
    & (Join-Path $PSScriptRoot 'build.ps1')
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

foreach ($required in ('TelegramDownloader.exe', 'UpdateHelper.exe', 'runtime-manifest.json')) {
    if (-not (Test-Path -LiteralPath (Join-Path $appDir $required) -PathType Leaf)) {
        throw "Packaged runtime is missing $required"
    }
}

if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) {
    $innoInstaller = Assert-ProjectChild (Join-Path $downloadCache 'innosetup-7.0.2-x64.exe')
    $expectedHash = '5ad54ca3def786f8f4212552e54cc6d8d61329e2d24a1cfee0571d42c2684ff1'
    if (-not (Test-Path -LiteralPath $innoInstaller -PathType Leaf) -or
        (Get-FileHash -Algorithm SHA256 -LiteralPath $innoInstaller).Hash.ToLowerInvariant() -ne $expectedHash) {
        Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/jrsoftware/issrc/releases/download/is-7_0_2/innosetup-7.0.2-x64.exe' -OutFile $innoInstaller
    }
    $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $innoInstaller).Hash.ToLowerInvariant()
    if ($actualHash -ne $expectedHash) {
        throw "Inno Setup archive hash mismatch: $actualHash"
    }
    $signature = Get-AuthenticodeSignature -FilePath $innoInstaller
    if ($signature.Status -ne 'Valid' -or
        $signature.SignerCertificate.Subject -notmatch 'Pyrsys B\.V\.') {
        throw 'Inno Setup Authenticode signature is not valid for Pyrsys B.V.'
    }
    if (Test-Path -LiteralPath $compilerRoot) {
        $resolvedCompilerRoot = [IO.Path]::GetFullPath($compilerRoot)
        if (-not $resolvedCompilerRoot.StartsWith($projectPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Compiler cleanup escaped project: $resolvedCompilerRoot"
        }
        Remove-Item -LiteralPath $resolvedCompilerRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $compilerRoot | Out-Null
    $quotedCompilerRoot = '"' + $compilerRoot + '"'
    $install = Start-Process -FilePath $innoInstaller -ArgumentList @(
        '/PORTABLE=1', '/CURRENTUSER', '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
        "/DIR=$quotedCompilerRoot"
    ) -WorkingDirectory $buildTemp -Wait -PassThru -WindowStyle Hidden
    if ($install.ExitCode -ne 0) {
        throw "Inno Setup portable extraction failed: $($install.ExitCode)"
    }
}

if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) {
    throw "Inno Setup compiler missing: $compiler"
}

if (Test-Path -LiteralPath $releaseDir) {
    $resolvedRelease = [IO.Path]::GetFullPath($releaseDir)
    if (-not $resolvedRelease.StartsWith($projectPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Release cleanup escaped project: $resolvedRelease"
    }
    Remove-Item -LiteralPath $resolvedRelease -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $releaseDir | Out-Null

$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$version = & $python -c "from telegram_downloader import __version__; print(__version__)"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$compileLog = Assert-ProjectChild (Join-Path $buildTemp 'iscc.log')
$versionDefinition = '/DAppVersion="' + $version + '"'
$sourceDefinition = '/DSourceDir="' + $appDir + '"'
$outputDefinition = '/DOutputDir="' + $releaseDir + '"'
& $compiler $versionDefinition $sourceDefinition $outputDefinition (Join-Path $projectRoot 'installer\TelegramDownloader.iss') 2>&1 | Tee-Object -FilePath $compileLog
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$setup = Assert-ProjectChild (Join-Path $releaseDir "TelegramDownloader-$version-win-x64-setup.exe")
if (-not (Test-Path -LiteralPath $setup -PathType Leaf)) {
    throw "Installer output missing: $setup"
}
& (Join-Path $PSScriptRoot 'smoke-installer.ps1') -SetupPath $setup
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Output $setup
