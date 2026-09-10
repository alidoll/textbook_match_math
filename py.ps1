param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "scripts\_venv.ps1")
$py = Get-TextbookMatchPython
& $py @Rest
exit $LASTEXITCODE
