# Cài môi trường Python trên Windows (PowerShell), chạy từ thư mục gốc của repo:
#   powershell -ExecutionPolicy Bypass -File deployment\scripts\setup-windows.ps1
$ErrorActionPreference = "Stop"
$py = "py"
& $py -3.12 --version
if ($LASTEXITCODE -ne 0) { throw "Cần Python 3.12 (python.org, chọn 'Add to PATH' và 'py launcher')" }
if (-not (Test-Path .venv)) { & $py -3.12 -m venv .venv }
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements\dev.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
if (-not (Test-Path .env)) { Copy-Item deployment\.env.example .env; Write-Host "Đã tạo .env: hãy sửa mật khẩu/token" }
if (-not (Test-Path secrets\model_signing_key.pem)) { .\.venv\Scripts\python.exe tools\keys.py init }
Write-Host "Xong. Chạy backend: .\.venv\Scripts\python.exe -m uvicorn presence_server.main:app --host 0.0.0.0 --port 8000"
