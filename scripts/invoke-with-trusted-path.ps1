function Invoke-WithTrustedPath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [ValidateNotNullOrEmpty()]
        [string[]]$TrustedPath,

        [Parameter(Mandatory = $true)]
        [ValidateNotNullOrEmpty()]
        [string]$FilePath,

        [string[]]$ArgumentList = @(),

        [Parameter(Mandatory = $true)]
        [ref]$ExitCode
    )

    $inheritedPath = $env:PATH
    try {
        $env:PATH = $TrustedPath -join [IO.Path]::PathSeparator
        & $FilePath @ArgumentList
        $ExitCode.Value = $LASTEXITCODE
    } finally {
        $env:PATH = $inheritedPath
    }
}
