param([Parameter(ValueFromRemainingArguments=$true)][string[]]$WorkbenchArgs)
$ErrorActionPreference = 'Stop'
$WorkbenchRoot = $PSScriptRoot
if ($env:BOBR_PYTHON) { $WorkbenchPython = $env:BOBR_PYTHON }
elseif (Test-Path -LiteralPath (Join-Path $WorkbenchRoot 'core/config.json')) {
    $WorkbenchPython = (Get-Content -Raw -LiteralPath (Join-Path $WorkbenchRoot 'core/config.json') | ConvertFrom-Json).python_executable
}
else { $WorkbenchPython = (Get-Command python -ErrorAction Stop).Source }
if (-not $WorkbenchPython -or -not (Test-Path -LiteralPath $WorkbenchPython -PathType Leaf) -or $WorkbenchPython.Replace([char]92,[char]47).ToLowerInvariant().Contains('/windowsapps/')) {
    throw 'Set BOBR_PYTHON to an existing interpreter executable; WindowsApps install-manager aliases are not executed.'
}
& $WorkbenchPython -B -X utf8 (Join-Path $WorkbenchRoot 'bobr.py') @WorkbenchArgs
exit $LASTEXITCODE
