# 首次安装：创建 .venv 并 pip install -r requirements.txt
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "scripts\_venv.ps1")
$py = Get-TextbookMatchPython -AllowCreate
Write-Host "完成: $py"
& $py -m pip list | Select-String -Pattern "Flask|mysql-connector|SQLAlchemy"
