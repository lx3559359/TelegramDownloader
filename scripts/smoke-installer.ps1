param(
    [Parameter(Mandatory = $true)]
    [string]$SetupPath
)

$ErrorActionPreference = 'Stop'

$projectRoot = (Resolve-Path (Split-Path -Parent $PSScriptRoot)).Path
$projectPrefix = $projectRoot.TrimEnd('\') + '\'

function Assert-ProjectChild([string]$Candidate) {
    $resolved = [IO.Path]::GetFullPath($Candidate)
    if (-not $resolved.StartsWith($projectPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Installer smoke path escaped project: $resolved"
    }
    return $resolved
}

$setup = (Resolve-Path -LiteralPath $SetupPath).Path
if (-not $setup.StartsWith($projectPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Installer escaped project: $setup"
}
$expectedName = 'TelegramDownloader-0.1.0-win-x64-setup.exe'
if ([IO.Path]::GetFileName($setup) -ne $expectedName) {
    throw "Installer version/name mismatch: $setup"
}

$smokeRoot = Assert-ProjectChild (Join-Path $projectRoot '.build-temp\installer-smoke')
$installDir = Assert-ProjectChild (Join-Path $projectRoot '.build-temp\installed-smoke')
$env:TEMP = Assert-ProjectChild (Join-Path $smokeRoot 'temp')
$env:TMP = $env:TEMP
$env:APPDATA = Assert-ProjectChild (Join-Path $smokeRoot 'user-profile\Roaming')
$env:LOCALAPPDATA = Assert-ProjectChild (Join-Path $smokeRoot 'user-profile\Local')
New-Item -ItemType Directory -Force -Path $smokeRoot, $env:TEMP, $env:APPDATA, $env:LOCALAPPDATA | Out-Null

if (Test-Path -LiteralPath $installDir) {
    $resolvedInstall = [IO.Path]::GetFullPath($installDir)
    if (-not $resolvedInstall.StartsWith($projectPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Install cleanup escaped project: $resolvedInstall"
    }
    Remove-Item -LiteralPath $resolvedInstall -Recurse -Force
}

$cReject = "C:\TelegramDownloader-Installer-Rejection-Smoke-$([Guid]::NewGuid().ToString('N'))"
if (Test-Path -LiteralPath $cReject) {
    throw "C-drive rejection target unexpectedly exists: $cReject"
}
$quotedCReject = '"' + $cReject + '"'
$cLog = Assert-ProjectChild (Join-Path $smokeRoot 'reject-c-drive.log')
$quotedCLog = '"' + $cLog + '"'
$rejected = Start-Process -FilePath $setup -ArgumentList @(
    '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/DIR=$quotedCReject", "/LOG=$quotedCLog"
) -WorkingDirectory $smokeRoot -Wait -PassThru -WindowStyle Hidden
if ($rejected.ExitCode -eq 0) {
    throw 'Installer unexpectedly accepted a C-drive target'
}
if (Test-Path -LiteralPath (Join-Path $cReject 'TelegramDownloader.exe')) {
    throw 'Installer copied application files to C before rejecting the target'
}

$quotedInstallDir = '"' + $installDir + '"'
$installLog = Assert-ProjectChild (Join-Path $smokeRoot 'install.log')
$quotedInstallLog = '"' + $installLog + '"'
$installArguments = @(
    '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
    "/DIR=$quotedInstallDir", "/LOG=$quotedInstallLog"
)
$installed = Start-Process -FilePath $setup -ArgumentList $installArguments -WorkingDirectory $smokeRoot -Wait -PassThru -WindowStyle Hidden
if ($installed.ExitCode -ne 0) {
    throw "Installer smoke installation failed: $($installed.ExitCode)"
}

$exe = Join-Path $installDir 'TelegramDownloader.exe'
foreach ($required in ($exe, (Join-Path $installDir 'UpdateHelper.exe'), (Join-Path $installDir 'runtime-manifest.json'))) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Installed runtime is missing: $required"
    }
}
$selfTest = Start-Process -FilePath $exe -ArgumentList '--self-test' -WorkingDirectory $installDir -Wait -PassThru -WindowStyle Hidden
if ($selfTest.ExitCode -ne 0) {
    throw "Installed self-test failed: $($selfTest.ExitCode)"
}
$reportPath = Join-Path $installDir 'data\logs\self-test.json'
$report = Get-Content -Raw -Encoding UTF8 -LiteralPath $reportPath | ConvertFrom-Json
$installPrefix = $installDir.TrimEnd('\') + '\'
foreach ($entry in $report.writable_paths.PSObject.Properties) {
    $resolved = [IO.Path]::GetFullPath([string]$entry.Value)
    if (-not $resolved.StartsWith($installPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Installed writable path escaped target: $resolved"
    }
}

$sentinel = Join-Path $installDir 'data\sentinel.keep'
Set-Content -LiteralPath $sentinel -Value 'preserve-on-upgrade-and-uninstall' -Encoding UTF8
$upgraded = Start-Process -FilePath $setup -ArgumentList $installArguments -WorkingDirectory $smokeRoot -Wait -PassThru -WindowStyle Hidden
if ($upgraded.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $sentinel -PathType Leaf)) {
    throw 'Installer upgrade did not preserve project-local data'
}

$uninstaller = Join-Path $installDir 'unins000.exe'
$uninstallLog = Assert-ProjectChild (Join-Path $smokeRoot 'uninstall.log')
$quotedUninstallLog = '"' + $uninstallLog + '"'
$uninstalled = Start-Process -FilePath $uninstaller -ArgumentList @(
    '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/LOG=$quotedUninstallLog"
) -WorkingDirectory $installDir -Wait -PassThru -WindowStyle Hidden
if ($uninstalled.ExitCode -ne 0) {
    throw "Installer smoke uninstall failed: $($uninstalled.ExitCode)"
}
if (-not (Test-Path -LiteralPath $sentinel -PathType Leaf)) {
    throw 'Normal uninstall removed user data'
}
if (Test-Path -LiteralPath $exe) {
    throw 'Normal uninstall did not remove managed runtime files'
}
Write-Output 'INSTALLER_SMOKE_OK'
