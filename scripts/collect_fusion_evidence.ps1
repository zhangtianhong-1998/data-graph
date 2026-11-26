[CmdletBinding()]
param(
    [string] $Repo = 'D:\ict\data-graph-main',
    [string] $Archive,
    [string] $ReviewRunFile,
    [string] $Output
)
$ErrorActionPreference = 'Stop'
$Repo = (Resolve-Path -LiteralPath $Repo).Path
if (-not $Archive) { $Archive = Join-Path $Repo '.cac/tmp/agent-fusion-20261125-2340' }
$rid = 'run_0cbda0cfc191412b991d3af6'
if (-not $ReviewRunFile) {
    $ws = Join-Path $Archive 'ws_d'
    $found = @(Get-ChildItem -LiteralPath $ws -Directory | ForEach-Object {
        $p = Join-Path $_.FullName "runs/$rid.json"
        if (Test-Path -LiteralPath $p -PathType Leaf) { $p }
    })
    $direct = Join-Path $ws "runs/$rid.json"
    if (Test-Path -LiteralPath $direct -PathType Leaf) { $found += $direct }
    if ($found.Count -ne 1) { throw 'Locate the reviewed run from the archive and pass its runs JSON via -ReviewRunFile.' }
    $ReviewRunFile = $found[0]
}
if ([IO.Path]::GetFileNameWithoutExtension($ReviewRunFile) -ne $rid) { throw 'Unexpected review run ID.' }
if (-not $Output) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $Output = Join-Path $Repo ".cac/tmp/fe-$stamp"
}
if (Test-Path -LiteralPath $Output) { throw 'Choose a new output directory.' }
$python = Join-Path $Repo '.venv/Scripts/python.exe'
$original = $env:PYTHONPATH
try {
    $env:PYTHONPATH = (Join-Path $Repo 'scripts') + [IO.Path]::PathSeparator + (Join-Path $Repo 'src')
    & $python -B -X utf8 (Join-Path $PSScriptRoot 'collect_fusion_evidence.py') --review-run $ReviewRunFile --det-census (Join-Path $Archive 't4_census_det') --review-census (Join-Path $Archive 't4_census_agent2') --truth (Join-Path $Archive 't4_truth.json') --output (Join-Path $Output 'computed')
    if ($LASTEXITCODE -ne 0) { throw "Collector exited with code $LASTEXITCODE." }
    Write-Output "Local working directory: $Output"
} finally {
    $env:PYTHONPATH = $original
}
