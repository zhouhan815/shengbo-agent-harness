$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
& '.\.venv\Scripts\python.exe' '.\launcher.py' --no-browser
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Output '声播与播前助手已启动：http://127.0.0.1:8765/'
