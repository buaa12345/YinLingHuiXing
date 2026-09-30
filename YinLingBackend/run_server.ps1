# 启动银龄慧行后端
# 用法：在 YinLingBackend 目录下运行  .\run_server.ps1
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# 若需要可在此设置 MySQL 密码环境变量，或直接在 config.py 中修改 DB_PASSWORD
# $env:YINLING_DB_PASSWORD = "你的mysql密码"

.\venv\Scripts\python.exe app.py
