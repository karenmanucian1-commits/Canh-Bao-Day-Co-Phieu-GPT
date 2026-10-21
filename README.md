# BOT B — Cổ phiếu vùng đáy

Quét KBS theo 6 điều kiện bắt buộc: RSI14<30, RSI7<30, MFI14<20, MFI11<20, KL TB20>10.000, và ít nhất 15/20 phiên có giao dịch. Phân tích tất cả mã đạt, không tự loại mã rủi ro.

## GitHub Actions

- 06:00 giờ Việt Nam, thứ Hai–Sáu: dùng cache giá gần nhất nếu có.
- 11:31 giờ Việt Nam, thứ Hai–Sáu: cập nhật giá và quét lại để có dữ liệu mới.
- GitHub schedule chạy theo UTC, có thể trễ vài phút hoặc hơn, không cam kết chính xác tuyệt đối.
- Lần chạy đầu hoặc cache hết hạn có thể quét toàn thị trường cả buổi sáng.

Trong Settings → Secrets and variables → Actions, tạo 2 repository secrets: `BOT_TOKEN`, `CHAT_ID`. Không commit `.env`.

Trong Actions → BOT B → Run workflow, chọn `test` để kiểm tra Telegram (không quét); chọn `morning` hoặc `noon` để chạy toàn bộ. Các kết quả CSV/TXT tải tại Artifacts của workflow.

Nếu workflow chưa được kích hoạt, vào tab Actions và bật Actions. GitHub có thể tự tắt scheduled workflows khi repository công khai không hoạt động lâu ngày.
