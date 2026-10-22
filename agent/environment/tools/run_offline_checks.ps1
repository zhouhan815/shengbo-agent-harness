$ErrorActionPreference = 'Stop'
$project = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$python = Join-Path $project '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Project virtual environment not found.' }

$dependencyOutput = & $python -m pip --isolated check
$dependencyStatus = $LASTEXITCODE
[System.IO.File]::WriteAllLines((Join-Path $project 'environment\reports\pip-check.txt'), [string[]]$dependencyOutput, (New-Object System.Text.UTF8Encoding($false)))
$dependencyOutput | Write-Output
if ($dependencyStatus -ne 0) { exit $dependencyStatus }
& $python -I (Join-Path $project 'environment\tests\offline_sdk_probe.py')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $python -I (Join-Path $project 'environment\tools\write_installation_report.py')
exit $LASTEXITCODE
