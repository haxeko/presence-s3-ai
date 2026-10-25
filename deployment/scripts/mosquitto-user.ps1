# Tạo/đổi mật khẩu một người dùng MQTT trong deployment/mosquitto/config/passwd (dùng image Mosquitto).
#   .\deployment\scripts\mosquitto-user.ps1 presence-backend
#   .\deployment\scripts\mosquitto-user.ps1 s3-a1b2c3          # tên = mã thiết bị
param([Parameter(Mandatory = $true)][string]$User)
$ErrorActionPreference = "Stop"
$cfg = Join-Path $PSScriptRoot "..\mosquitto\config" | Resolve-Path
$pw = Read-Host "Mật khẩu cho $User" -AsSecureString
$plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($pw))
if (-not (Test-Path "$cfg\passwd")) { New-Item -ItemType File "$cfg\passwd" | Out-Null }
docker run --rm -v "${cfg}:/mosquitto/config" eclipse-mosquitto:2.0.22 mosquitto_passwd -b /mosquitto/config/passwd $User $plain
Write-Host "Đã lưu. Khởi động lại broker: docker compose -f deployment/docker-compose.yml restart mosquitto"
