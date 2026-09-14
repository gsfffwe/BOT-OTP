# Hướng dẫn tích hợp API thuê số OTP

## 1. Bắt đầu

1. Mở chat riêng với bot, gửi `/start`, sau đó `/api` hoặc chọn **API cho khách**.
2. Chọn **Tạo API key** và lưu key vào nơi riêng tư. Key chỉ được hiển thị đầy đủ khi tạo/đổi; không thể xem lại key cũ.
3. Nạp tiền trong bot. API dùng **chính số dư tài khoản Telegram đó**, không cần nạp vào một ví khác.
4. Lấy danh sách dịch vụ, chọn `app_id`, thuê số, rồi kiểm tra đơn để lấy OTP.

Base URL của bot:

```text
{{BASE_URL}}
```

Các đường dẫn bên dưới nối sau Base URL. Chỉ gọi qua HTTPS khi dùng thật.
Nếu vẫn thấy `TEN-MIEN-BOT-OTP`, hãy liên hệ quản trị viên để cấu hình địa chỉ API trước khi tích hợp.

Mọi yêu cầu cần header:

```http
Authorization: Bearer YOUR_API_KEY
```

Không gửi key trong URL, ảnh chụp, mã nguồn công khai hoặc JavaScript phía trình duyệt. Ai có key có thể thuê số bằng số dư của bạn. Nếu lộ key, vào `/api` để **Đổi API key** hoặc **Thu hồi API key** ngay. Thay key không xoá số dư hay các đơn đang xử lý; key mới vẫn xem được đơn cũ của chính bạn.

## 2. Các lệnh

| Phương thức | Đường dẫn | Công dụng |
| --- | --- | --- |
| GET | `/balance` | Xem số dư dùng chung với bot |
| GET | `/services` | Danh sách dịch vụ và giá bán hiện tại |
| POST | `/orders` | Thuê số; trừ tiền từ ví |
| GET | `/orders/ORDER_ID` | Xem số điện thoại, trạng thái và OTP |
| GET | `/orders?limit=20&offset=0` | Lịch sử đơn tạo qua API của bạn |

`limit`: 1–100. `offset`: từ 0. Danh sách đơn mới nhất đứng trước.
Không cần gửi Telegram ID; hệ thống xác định tài khoản từ API key. Không thể xem đơn của khách khác.

### Xem số dư

```bash
curl "{{BASE_URL}}/balance" -H "Authorization: Bearer YOUR_API_KEY"
```

Ví dụ phản hồi:

```json
{"success":true,"data":{"balance":20000,"currency":"VND"}}
```

### Xem dịch vụ

```bash
curl "{{BASE_URL}}/services" -H "Authorization: Bearer YOUR_API_KEY"
```

```json
{"success":true,"data":{"services":[{"app_id":1001,"name":"Facebook","price":5000,"currency":"VND","category":"social"}]}}
```

ID, giá và số dư trong tài liệu chỉ là ví dụ. Luôn dùng `app_id` và `price` từ kết quả hiện tại. Giá thuê API theo giá bán đang cấu hình trong bot, đơn vị VND, số nguyên.

### Thuê số

```http
POST /orders
Authorization: Bearer YOUR_API_KEY
Content-Type: application/json
Idempotency-Key: rental-20260914-0001
```

```json
{"app_id":1001,"max_price":5000}
```

| Trường | Bắt buộc | Ý nghĩa |
| --- | --- | --- |
| `app_id` | Có | ID dịch vụ, số nguyên dương |
| `max_price` | Không | Giá tối đa chấp nhận, số nguyên dương. Nếu giá hiện tại cao hơn: trả lỗi, không trừ tiền |
| `carrier` | Không | `Viettel`, `Mobi`, `Vina`, `VNMB` hoặc `ITelecom`; bỏ qua để tìm ngẫu nhiên |
| `phone` | Không | Chuỗi số Việt Nam cần thuê, ví dụ `"0912345678"`; không đảm bảo có sẵn trong kho |

Chỉ gửi các trường được liệt kê, không gửi `user_id`, số tiền tự chọn hay số dư.

Ví dụ đã cấp số:

```json
{
  "success": true,
  "data": {
    "order_id": "API-0123456789ABCDEF",
    "app_id": 1001,
    "app_name": "Facebook",
    "price": 5000,
    "currency": "VND",
    "status": "waiting",
    "phone": "0912345678",
    "otp": null,
    "expires_at": 1789340820,
    "created_at": "2026-09-14T00:20:00Z",
    "error_code": null,
    "balance": 15000
  }
}
```

`expires_at` là Unix timestamp theo giây, thường 7 phút sau khi cấp số. `balance` là số dư hiện tại tại lúc đọc phản hồi, không phải ảnh chụp cố định của từng đơn.

**Chống mua/trừ tiền trùng:** mỗi đơn mới phải có `Idempotency-Key` riêng (8–128 ký tự chữ/số hoặc `_ . : -`). Hãy lưu mã này cùng nội dung yêu cầu trước khi gửi.

- Nếu mất mạng hoặc timeout: gửi lại **cùng key và cùng nội dung**, hệ thống trả lại đơn đã ghi nhận, không tạo thêm lần thuê.
- Dùng lại key với nội dung khác trả `409 IDEMPOTENCY_CONFLICT`.
- Đơn đã thất bại vẫn giữ key cũ. Chỉ tạo key mới khi bạn thực sự muốn mua một đơn mới.
- Không tự tạo key mới khi đơn đang `allocating` hoặc `review`.

### Lấy OTP

```bash
curl "{{BASE_URL}}/orders/API-0123456789ABCDEF" -H "Authorization: Bearer YOUR_API_KEY"
```

Khi có mã: `data.status` là `success`, `data.otp` chứa mã OTP. Giữ OTP ở dạng chuỗi để không mất số 0 đầu. Kiểm tra cách nhau ít nhất **7 giây** khi đang chờ; các lần GET không trừ tiền. Đơn tạo qua API cập nhật kết quả qua API, không tự gửi từng OTP vào chat Telegram.

## 3. Trạng thái và thanh toán

| `status` | Ý nghĩa | Tiền trong ví |
| --- | --- | --- |
| `allocating` | Đã nhận yêu cầu, đang tìm số | Đã giữ/trừ số tiền thuê |
| `waiting` | Đã cấp số, đang chờ OTP | Đã trừ tiền |
| `success` | Đã nhận OTP | Không trừ thêm |
| `refunded` | Hết phiên, chưa nhận OTP | Đã hoàn vào cùng ví |
| `failed` | Yêu cầu cấp số bị từ chối, hoặc đã được admin kiểm tra và hoàn | Đã hoàn vào cùng ví |
| `review` | Chưa xác định được kết quả cấp số do lỗi kết nối/xử lý | Giữ tiền chờ admin kiểm tra; không tự mua lại |

Khi nhận POST hợp lệ và đủ số dư, hệ thống ghi đơn và trừ tiền cùng một giao dịch. Không có số thì hoàn lại; hết phiên không có OTP thì hoàn lại một lần. Nếu đang mất kết nối, hệ thống đợi kiểm tra được kết quả trước khi hoàn, tránh vừa cấp OTP vừa hoàn tiền.

HTTP `200` cùng `success: true` nghĩa là đã đọc/xử lý được yêu cầu, **không đảm bảo đã có OTP**; luôn kiểm tra `data.status`. Đơn `allocating`/`review` được trả HTTP `202` khi gọi POST. Gọi GET để theo dõi hoặc liên hệ admin kèm `order_id` nếu đơn cần kiểm tra.

## 4. Lỗi và giới hạn

Lỗi nghiệp vụ có dạng:

```json
{"success":false,"error":{"code":"INSUFFICIENT_BALANCE","message":"Số dư chưa đủ. Vui lòng nạp tiền trong bot."}}
```

| HTTP | Mã lỗi | Cách xử lý |
| --- | --- | --- |
| 400 | `IDEMPOTENCY_KEY_REQUIRED` | Bổ sung/sửa header Idempotency-Key |
| 401 | `INVALID_API_KEY` | Kiểm tra Bearer key; key cũ ngừng hoạt động khi đổi/thu hồi |
| 402 | `INSUFFICIENT_BALANCE` | Nạp tiền trong bot |
| 404 | `SERVICE_NOT_FOUND`, `ORDER_NOT_FOUND` | Kiểm tra ID và tài khoản sở hữu đơn |
| 409 | `PRICE_CHANGED` | Xem giá mới và xác nhận mức giá chấp nhận |
| 409 | `IDEMPOTENCY_CONFLICT` | Không dùng một mã yêu cầu cho hai nội dung khác nhau |
| 422 | `INVALID_PHONE` hoặc `detail` | Kiểm tra kiểu dữ liệu, số điện thoại và các trường gửi lên |
| 429 | `RATE_LIMITED` | Chờ theo header Retry-After (60 giây) |
| 503 | `SERVICES_UNAVAILABLE` | Danh mục tạm thời chưa tải được; thử lại sau |

Tối đa **120 yêu cầu/phút/tài khoản** trên một tiến trình bot, tính chung tất cả endpoint. Đổi key không đặt lại giới hạn. Lỗi kiểm tra dữ liệu tự động có thể trả `{"detail":[...]}` thay vì khung lỗi nghiệp vụ.

`data.error_code` có thể là `NUMBER_UNAVAILABLE` (đã hoàn tiền), `ORDER_REVIEW` (chờ kiểm tra), `ADMIN_REFUNDED` (admin đã hoàn). Không tạo lại đơn tự động chỉ vì một phản hồi có `error_code`.

## 5. Ví dụ Python

Cài thư viện: `pip install httpx`. Đặt API key riêng vào biến môi trường `OTP_CUSTOMER_API_KEY`. Ví dụ này tạo **một đơn có tính tiền**, nên chọn đúng dịch vụ trước khi chạy.

```python
import os
import time
import uuid
import httpx

BASE_URL = "{{BASE_URL}}"
API_KEY = os.environ["OTP_CUSTOMER_API_KEY"]

with httpx.Client(
    headers={"Authorization": f"Bearer {API_KEY}"}, timeout=40
) as client:
    catalog = client.get(BASE_URL + "/services")
    catalog.raise_for_status()
    services = catalog.json()["data"]["services"]
    for item in services:
        print(item["app_id"], item["name"], item["price"])

    app_id = int(input("ID dịch vụ cần thuê: "))
    service = next(item for item in services if item["app_id"] == app_id)
    body = {"app_id": app_id, "max_price": service["price"]}
    request_key = str(uuid.uuid4())
    print("Lưu mã yêu cầu và nội dung trước khi gửi:", request_key, body)
    # Ứng dụng thật phải lưu bền vững request_key + body trước bước POST.
    # Nếu timeout: dùng lại chính request_key + body; KHÔNG chạy lại từ uuid4().
    response = client.post(
        BASE_URL + "/orders", json=body,
        headers={"Idempotency-Key": request_key},
    )
    response.raise_for_status()
    order = response.json()["data"]
    print("Mã đơn:", order["order_id"], "Số:", order["phone"])

    # Giới hạn thời gian theo dõi trong ví dụ. Dừng script không huỷ đơn.
    for _ in range(90):
        if order["status"] not in ("allocating", "waiting"):
            break
        time.sleep(7)
        response = client.get(BASE_URL + "/orders/" + order["order_id"])
        response.raise_for_status()
        order = response.json()["data"]
    print("Trạng thái:", order["status"], "OTP:", order["otp"])
    # Còn waiting: theo dõi tiếp bằng GET. review: liên hệ admin, không mua lại.
```

API này dùng để thuê số và nhận OTP cho các mục đích hợp pháp, tuân thủ điều khoản của dịch vụ liên quan.
