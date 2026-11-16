<# Статическая проверка; входную миссию не меняет. Политика задаётся явно. #>
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Mission,
    [Parameter(Mandatory=$true)][string]$Policy,
    [Parameter(Mandatory=$true)][string]$Output,
    [Parameter(Mandatory=$true)][string]$PythonExe,
    [string]$Baseline,
    [string]$Catalog,
    [string]$Definitions
)
$ErrorActionPreference = "Stop"
$auditArgs = @("-B", "-X", "utf8", (Join-Path $PSScriptRoot "dayz_audit.py"),
    "--mission", $Mission, "--policy", $Policy, "--output", $Output)
foreach ($option in @("Baseline", "Catalog", "Definitions")) {
    $value = Get-Variable -Name $option -ValueOnly
    if ($value) { $auditArgs += @(("--" + $option.ToLowerInvariant()), $value) }
}
& $PythonExe @auditArgs
exit $LASTEXITCODE
