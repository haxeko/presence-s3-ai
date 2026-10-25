# CA riêng cho TLS (tùy chọn)

Mặc định, firmware kiểm tra chứng chỉ của broker (`mqtts://`) và backend (`https://`) bằng bộ chứng chỉ gốc công khai
của ESP-IDF. Cách này dùng được với chứng chỉ của Let's Encrypt hoặc CA thương mại cho một tên miền.

Khi dùng CA tự tạo (broker/backend trong LAN), chép chứng chỉ **CA** (không phải khóa riêng) vào `firmware/certs/ca.pem`,
rồi chạy `idf.py reconfigure build`. Firmware sẽ chỉ tin CA này, và `status.tls_trust` báo `private_ca`.

Chứng chỉ máy chủ phải có tên (SAN) đúng địa chỉ thiết bị dùng để kết nối: tên miền, hoặc IP (`IP:192.168.1.10`).
Xem [docs/security.md](../../docs/security.md). File `*.pem` ở đây không được commit (`.gitignore`).
