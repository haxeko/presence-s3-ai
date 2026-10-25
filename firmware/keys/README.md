# Khóa ký gói mô hình

`model_signing_pub.pem` (khóa công khai, ECDSA P-256) được nhúng vào firmware khi
`CONFIG_PRESENCE_MODEL_REQUIRE_SIGNATURE=y` (mặc định). Tạo cặp khóa một lần:

```powershell
python tools/keys.py init
```

Lệnh tạo `secrets/model_signing_key.pem` (khóa RIÊNG, chỉ để ở máy chạy backend/huấn luyện, không commit) và
file khóa công khai ở thư mục này. Cả hai đều bị `.gitignore` bỏ qua: mỗi bản cài đặt có khóa riêng.
