$ErrorActionPreference = 'Stop'

$projectRoot = (Resolve-Path (Split-Path -Parent $PSScriptRoot)).Path
$appDir = (Resolve-Path (Join-Path $projectRoot 'dist\TelegramDownloader')).Path
$projectPrefix = $projectRoot.TrimEnd('\') + '\'
if (-not $appDir.StartsWith($projectPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Packaged directory escaped project: $appDir"
}

$exe = Join-Path $appDir 'TelegramDownloader.exe'
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
    throw "Packaged executable missing: $exe"
}
$process = Start-Process -FilePath $exe -ArgumentList '--self-test' -WorkingDirectory $appDir -Wait -PassThru -WindowStyle Hidden
if ($process.ExitCode -ne 0) {
    throw "Self-test exited $($process.ExitCode)"
}

$reportPath = Join-Path $appDir 'data\logs\self-test.json'
$report = Get-Content -Raw -Encoding UTF8 -LiteralPath $reportPath | ConvertFrom-Json
if (-not $report.ok) {
    throw 'Packaged path self-test failed'
}
$appPrefix = $appDir.TrimEnd('\') + '\'
foreach ($entry in $report.writable_paths.PSObject.Properties) {
    $resolved = [IO.Path]::GetFullPath([string]$entry.Value)
    if (-not $resolved.StartsWith($appPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Path escaped package: $resolved"
    }
}
Write-Output 'PACKAGED_SMOKE_OK'
