# Triển khai API khách hàng — BOT OTP

Phạm vi thay đổi nằm trong thư mục BOT OTP. Không yêu cầu sửa web_test hay BOT BAN TAI KHOAN API và không đổi đường dẫn SePay hiện có.

## Các file phải có khi deploy

- `test_otp.py`: bot, ví và tích hợp API.
- `customer_api.py`: xác thực khách, endpoint, chống mua trùng.
- `API_GUIDE.md`: tài liệu gửi khi khách bấm Hướng dẫn API; bắt buộc có cùng thư mục script.
- Giữ các file và thư viện hiện tại, bao gồm requirements và Procfile. Không cần cài thêm thư viện cho API này.

Chạy bằng lệnh hiện tại `python test_otp.py` để khởi tạo cơ sở dữ liệu và cả bot/web server. Không chạy thêm một bản bot hoặc nhiều worker cho cùng file SQLite/token Telegram.

## Domain và cấu hình

API dùng public HTTPS domain của **BOT OTP**, chung web server/PORT với webhook SePay. Không dùng domain của bot bán tài khoản, không lấy SEPAY_FORWARD_URL làm địa chỉ API.

Trên Railway, nếu dịch vụ đã có Public Networking và `RAILWAY_PUBLIC_DOMAIN`, không cần thêm biến. Nếu dùng domain riêng hoặc nơi khác, đặt:

```dotenv
CUSTOMER_API_BASE_URL=https://DOMAIN-CUA-BOT-OTP
```

Có thể điền sẵn `/api/v1` ở cuối; bot không thêm trùng. Giá trị này chỉ dùng hiển thị địa chỉ trong hướng dẫn; domain vẫn phải được định tuyến đúng vào dịch vụ đang chạy. Nếu chưa có domain public, tạo domain cho chính service BOT OTP và trỏ đúng PORT đang chạy. Không thay đổi cấu hình webhook/forward để bật API.

Sau deploy, vào chat riêng gửi `/api`. Kiểm tra URL hiển thị đúng, tạo key và gọi GET `/balance`, GET `/services` trước. Hai lệnh này không mua số. POST `/orders` là giao dịch thực, có tính tiền.

## Dữ liệu và thanh toán

Migration chỉ thêm bảng `customer_api_keys`, `customer_api_orders` và cột `otp_history.source`, không xoá/reset user, ví, đơn nạp hay lịch sử cũ. Key chỉ lưu SHA-256 và 6 ký tự cuối, không lưu key đầy đủ.

Giá API lấy từ cùng danh mục/giá bán của bot. Mỗi key thuộc một Telegram user và dùng `users.balance`. Không có ví API riêng, kể cả admin gọi API cũng tính phí theo ví. Các thao tác thuê trong bot và API cùng khoá mua của từng user; giao dịch trừ/hoàn API và mã chống mua trùng lưu vào SQLite.

Giữ/bảo toàn file SQLite đang dùng khi triển khai. Dữ liệu key, số dư và mã chống mua trùng phụ thuộc file này. Railway không gắn bộ nhớ lưu trữ lâu dài có thể mất file khi thay container; tính năng API không tự giải quyết giới hạn đó. Không commit `.db` hoặc key lên GitHub công khai. Dùng cơ chế backup/restore đang có và bản sao lưu nhất quán trước khi thay nơi chạy.

## Đơn cần kiểm tra

Nếu mất kết nối trong lúc cấp số, API đánh dấu `review` và giữ khoản tiền đã trừ, không tự cấp lại số. Admin nhận thông báo an toàn kèm mã đơn. Sau khởi động, các đơn còn `allocating` cũng chuyển thành `review` để tránh gửi lại yêu cầu mua không rõ kết quả.

Admin gửi `/api_review` trong chat riêng để xem tối đa 20 đơn cũ nhất đang cần kiểm tra. Đối chiếu kết quả thực tế trước khi chọn Hoàn và xác nhận. Chỉ hoàn nếu xác minh đơn chưa được cấp số/OTP; thao tác này chỉ hoàn một lần. Sau khi giải quyết các đơn đầu, danh sách sẽ hiện các đơn tiếp theo. Không hoàn đơn đã được giao chỉ vì khách báo timeout.

Các đơn đã có số tiếp tục được khôi phục theo dõi sau khi khởi động; hết phiên không có OTP thì hoàn tiền vào cùng ví. Mất mạng tạm thời không đồng nghĩa với hết hạn để hoàn tiền. Key đổi/thu hồi không huỷ các đơn đã tạo.

## Kiểm thử offline

```text
python -m unittest discover -s tests -p "test_*.py"
```

Các bài kiểm tra dùng DB tạm và giả lập Telegram, danh mục, cấp số, OTP. Không gọi dịch vụ thật, không trừ ví thật. Đây không thay thế việc kiểm tra domain/routing và cấu hình của bản triển khai thực tế.
