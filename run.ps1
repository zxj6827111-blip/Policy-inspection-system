$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw '尚未安装项目环境，请先运行 .\setup.ps1'
}
Set-Location $root
& $python -m uvicorn app.main:app --host 127.0.0.1 --port 8765

