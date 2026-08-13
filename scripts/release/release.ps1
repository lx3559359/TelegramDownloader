param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$')]
    [string]$Version
)

$ErrorActionPreference = 'Stop'

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$projectPrefix = $projectRoot.TrimEnd('\') + '\'

function Assert-ProjectChild([string]$Candidate) {
    $resolved = [IO.Path]::GetFullPath($Candidate)
    if (-not $resolved.StartsWith($projectPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Release path escaped project: $resolved"
    }
    return $resolved
}

function Assert-Succeeded([string]$Operation) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Operation failed with exit code $LASTEXITCODE"
    }
}

if ((git -C $projectRoot branch --show-current) -ne 'main') {
    throw 'Formal releases must run from main.'
}
if (git -C $projectRoot status --porcelain) {
    throw 'Formal releases require a clean worktree.'
}
$python = Assert-ProjectChild (Join-Path $projectRoot '.venv\Scripts\python.exe')
$packageVersion = & $python -c "from telegram_downloader import __version__; print(__version__)"
Assert-Succeeded 'Read source version'
if ($packageVersion -ne $Version) {
    throw "Source version does not match requested release: $packageVersion"
}
$privateKey = Assert-ProjectChild (Join-Path $projectRoot '.release-secrets\ed25519-private.pem')
$trustedKeys = Assert-ProjectChild (Join-Path $projectRoot 'src\telegram_downloader\trusted_update_keys.json')
if (-not (Test-Path -LiteralPath $privateKey -PathType Leaf)) {
    throw 'Project-local Ed25519 release private key is missing.'
}
foreach ($remote in ('github', 'modelscope')) {
    if ($remote -notin (git -C $projectRoot remote)) {
        throw "Git remote is missing: $remote"
    }
}
if (-not $env:MODELSCOPE_API_TOKEN) {
    throw 'MODELSCOPE_API_TOKEN is not set in the release process.'
}

$env:GITHUB_WORKSPACE = $projectRoot
$env:TEMP = Assert-ProjectChild (Join-Path $projectRoot '.local\temp\release')
$env:TMP = $env:TEMP
$env:MODELSCOPE_CACHE = Assert-ProjectChild (Join-Path $projectRoot '.local\cache\modelscope')
$env:MODELSCOPE_HOME = Assert-ProjectChild (Join-Path $projectRoot '.local\state\modelscope')
New-Item -ItemType Directory -Force -Path $env:TEMP,$env:MODELSCOPE_CACHE,$env:MODELSCOPE_HOME | Out-Null

& (Join-Path $projectRoot 'scripts\test.ps1')
Assert-Succeeded 'Tests'
& (Join-Path $projectRoot 'scripts\build-installer.ps1')
Assert-Succeeded 'Package build'

$candidate = Assert-ProjectChild (Join-Path $projectRoot "dist\release\v$Version")
if (Test-Path -LiteralPath $candidate) {
    Remove-Item -LiteralPath $candidate -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $candidate | Out-Null
$portable = Assert-ProjectChild (Join-Path $projectRoot "dist\TelegramDownloader-$Version-win-x64-portable.zip")
$installer = Assert-ProjectChild (Join-Path $projectRoot "dist\release\TelegramDownloader-$Version-win-x64-setup.exe")
Copy-Item -LiteralPath $portable -Destination $candidate
Copy-Item -LiteralPath $installer -Destination $candidate
$sourceArchive = Assert-ProjectChild (Join-Path $candidate "TelegramDownloader-$Version-source.zip")
git -C $projectRoot archive --format=zip --prefix="TelegramDownloader-$Version/" --output=$sourceArchive HEAD
Assert-Succeeded 'Source archive'

$notes = Assert-ProjectChild (Join-Path $projectRoot "docs\releases\v$Version.md")
$commitDate = git -C $projectRoot show -s --format=%cI HEAD
Assert-Succeeded 'Read commit date'
$publishedAt = [DateTimeOffset]::Parse($commitDate).UtcDateTime.ToString('yyyy-MM-ddTHH:mm:ssZ')
& $python -m scripts.release.generate_manifest --version $Version --published-at $publishedAt --release-notes $notes --portable $portable --installer $installer --private-key $privateKey --trusted-keys $trustedKeys --key-id 'release-2026-01' --output $candidate
Assert-Succeeded 'Signed manifest generation'

$githubRepo = gh repo view 'lx3559359/TelegramDownloader' --json nameWithOwner,visibility 2>$null
if ($LASTEXITCODE -ne 0) {
    gh repo create 'lx3559359/TelegramDownloader' --public --description 'Windows GUI Telegram media downloader with signed updates'
    Assert-Succeeded 'Create public GitHub repository'
} elseif (($githubRepo | ConvertFrom-Json).visibility -ne 'PUBLIC') {
    throw 'GitHub repository is not public.'
}
& $python -m scripts.release.publish_modelscope ensure-repo --version $Version --source $candidate
Assert-Succeeded 'Ensure public ModelScope repository'

$tag = "v$Version"
$existingTag = git -C $projectRoot tag --list $tag
if ($existingTag) {
    if ((git -C $projectRoot rev-list -n 1 $tag) -ne (git -C $projectRoot rev-parse HEAD)) {
        throw "Existing tag does not point to HEAD: $tag"
    }
} else {
    git -C $projectRoot tag -a $tag -m "TelegramDownloader $Version"
    Assert-Succeeded 'Create release tag'
}
git -C $projectRoot push github HEAD:main $tag
Assert-Succeeded 'Push GitHub main and tag'
git -C $projectRoot push modelscope HEAD:main $tag
Assert-Succeeded 'Push ModelScope main and tag'

& $python -m scripts.release.publish_github stage --version $Version --source $candidate --workspace $projectRoot
Assert-Succeeded 'Stage GitHub draft release'
& $python -m scripts.release.publish_modelscope stage --version $Version --source $candidate
Assert-Succeeded 'Stage ModelScope candidate'
& $python -m scripts.release.publish_github verify --version $Version --source $candidate --workspace $projectRoot
Assert-Succeeded 'Verify GitHub draft release'
& $python -m scripts.release.publish_modelscope verify --version $Version --source $candidate
Assert-Succeeded 'Verify ModelScope candidate'

$githubDownload = Assert-ProjectChild (Join-Path $projectRoot '.local\temp\release\github')
$modelscopeDownload = Assert-ProjectChild (Join-Path $projectRoot '.local\temp\release\modelscope')
$modelscopeExpected = Assert-ProjectChild (Join-Path $projectRoot '.local\temp\release\modelscope-expected')
& $python -m scripts.release.publish_github download --version $Version --source $candidate --workspace $projectRoot --destination $githubDownload
Assert-Succeeded 'Download GitHub candidate'
& $python -m scripts.release.publish_modelscope download --version $Version --source $candidate --destination $modelscopeDownload
Assert-Succeeded 'Download ModelScope candidate'
if (Test-Path -LiteralPath $modelscopeExpected) {
    Remove-Item -LiteralPath $modelscopeExpected -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $modelscopeExpected | Out-Null
$modelNames = @(
    "TelegramDownloader-$Version-source.zip",
    "TelegramDownloader-$Version-win-x64-portable.zip",
    "TelegramDownloader-$Version-win-x64-setup.exe",
    'release-notes.md',
    'update-manifest.json',
    'update-manifest.sig'
)
foreach ($name in $modelNames) {
    Copy-Item -LiteralPath (Join-Path $candidate $name) -Destination $modelscopeExpected
}
$githubNames = @($modelNames) + @('latest.json')
& $python -m scripts.release.verify_remote_release --expected $candidate --actual $githubDownload --names $githubNames
Assert-Succeeded 'Compare GitHub candidate bytes'
& $python -m scripts.release.verify_remote_release --expected $modelscopeExpected --actual $modelscopeDownload --names $modelNames
Assert-Succeeded 'Compare ModelScope candidate bytes'

$pointerBackup = Assert-ProjectChild (Join-Path $projectRoot '.local\state\release\modelscope-latest.json')
& $python -m scripts.release.publish_modelscope save-pointer --version $Version --source $candidate --pointer-backup $pointerBackup
Assert-Succeeded 'Save ModelScope pointer'
$modelPromoted = $false
try {
    & $python -m scripts.release.publish_modelscope promote --version $Version --source $candidate
    Assert-Succeeded 'Promote ModelScope pointer'
    $modelPromoted = $true
    & $python -m scripts.release.publish_github promote --version $Version --source $candidate --workspace $projectRoot
    Assert-Succeeded 'Publish GitHub release'
    & $python -m scripts.release.publish_modelscope verify-pointer --version $Version --source $candidate
    Assert-Succeeded 'Verify ModelScope pointer'
    & $python -m scripts.release.publish_github verify-pointer --version $Version --source $candidate --workspace $projectRoot
    Assert-Succeeded 'Verify GitHub pointer'
} catch {
    & $python -m scripts.release.publish_github demote --version $Version --source $candidate --workspace $projectRoot
    if ($modelPromoted) {
        & $python -m scripts.release.publish_modelscope restore-pointer --version $Version --source $candidate --pointer-backup $pointerBackup
    }
    throw
}

Write-Output "RELEASE_PUBLISHED v$Version"
