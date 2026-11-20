# Thiết kế API Test Executor (Phần 6)

## 1. Vai trò
`api_test_executor.py` nằm giữa Test Data Generator (Phần 5) và Result Validator (Phần 7).
Nó **chỉ thực thi và ghi nhận**: gửi request thật tới backend BShop theo từng bước của test case, lưu HTTP status, response, lỗi kỹ thuật và trạng thái quan sát được sau mỗi bước.
Nó **không** so sánh expected với actual, **không** kết luận PASS/FAIL và **không** kết luận lỗ hổng. `COMPLETED` chỉ có nghĩa là mọi bước đã được gửi; nó không có nghĩa là test đạt.

## 2. Luồng xử lý
`test_cases.json` → `SuiteLoader` (kiểm tra schema) → `TransitionApiMapper` (transition → endpoint thật) → `ApiTestExecutor` → `test_execution_results.json`.

| Thành phần | Trách nhiệm |
|---|---|
| `SuiteLoader`, `validate_case` | Đọc file Phần 5, kiểm tra schema v1.0, tính sha256 nguồn. Test case lỗi cấu trúc bị SKIPPED (`INVALID_TEST_CASE`), không làm dừng cả suite. |
| `TransitionApiMapper` | Đối chiếu request của mỗi bước với `api` của transition trong `efsm_model.json` và với bảng route thật lấy từ `app.openapi()`. Không tự bịa endpoint. Negative transition dùng endpoint của `base_transition`. |
| `ExecutionContext` | Lưu response từng bước, giải `$ref:steps.N.response.<path>` (ví dụ `order.id` từ T04), ghi `captures`. Không dùng ID cố định. |
| `TestClientTransport` | FastAPI TestClient, `raise_server_exceptions=False` để 5xx là một response chứ không phải exception. |
| `HttpxTransport` | Chế độ HTTP ngoài (cấu hình riêng). Timeout/ lỗi kết nối → `TransportError`. |
| `InProcessEnvironment` | Đặt `ONLINE_SHOP_DATABASE_URL` trỏ tới file SQLite tạm trước khi import `app`, từ chối `online_shop.db` và DB không phải SQLite, gọi `reset_database()` trước và sau mỗi test case, áp fixture. |
| `ExternalEnvironment` | Chạy vào server đang chạy; bắt buộc `--allow-dirty-target`, không reset, không fixture, ghi `isolation: NOT_GUARANTEED`. |
| `StateObserver` | Quan sát trạng thái qua GET `/api/cart`, `/api/products`, `/api/orders`, `/api/payments` (không đọc DB trực tiếp). |

## 3. Quy tắc xử lý kết quả
- Bước đã gửi có trạng thái `SENT` bất kể HTTP status. 4xx ở negative case là phản hồi nghiệp vụ, 5xx được ghi nhận như response; không dừng case vì status khác 2xx.
- Lỗi kỹ thuật (→ `EXECUTION_ERROR`): timeout, lỗi kết nối, exception transport, request không khớp EFSM/route, fixture thất bại, trạng thái ban đầu không khớp `initial_data` (`ENVIRONMENT_MISMATCH`).
- `PARTIAL`: có bước `NOT_SENT` (ví dụ `$ref` không giải được vì bước tạo đơn bị từ chối) hoặc không có bước nào.
- `SKIPPED`: readiness `BLOCKED`/không hỗ trợ, 49 path trong `unconverted_paths` (lý do `NO_FEASIBLE_DATA_FOUND`), test case không hợp lệ, hoặc cần fixture mà môi trường không áp được. Không gửi request nào.
- Mỗi bước có `expected_ref {test_case_id, step_no}` chỉ là con trỏ để Phần 7 tra expected trong `test_cases.json`; executor không đọc hay so sánh nội dung expected.
- Assertion probe (GET theo `assertions` của test case) chạy một lần cho mỗi URL, chỉ ghi lại phản hồi, không đánh giá.

## 4. Cô lập dữ liệu và bảo mật log
- Mỗi test case: reset → kiểm tra trạng thái ban đầu khớp `initial_data` → chạy → quan sát cuối → reset.
- `online_shop.db` không bao giờ được mở để ghi; `_assert_safe_database` ném `UnsafeEnvironmentError` nếu engine trỏ vào file này.
- Các trường `customer_name`, `phone`, `address` được che trong request/response/observed state (`<redacted len=N>`); giá trị thật không xuất hiện trong file kết quả.

## 5. Định dạng `test_execution_results.json`
`schema_version`, `executor`, `scope`, `source{test_cases, sha256, generator, schema_version}`, `environment`, `summary`, `results[]`.
Mỗi kết quả gồm: `test_case_id, path_id, path_key, test_type, readiness, negative_transition_id, skip_reason, steps[], errors[], isolation, initial_observed_state, assertion_probes[], final_observed_state, execution_status, duration_ms`.
Mỗi bước gồm: `step_no, role, kind, transition_id | negative_transition_id, expected_ref, mapped_from, request, response, captured, capture_missing, observed_state, observation_errors, duration_ms, error, execution_status`.

## 6. Chạy
```
python api_test_executor.py --test-cases test_cases.json --model efsm_model.json --output test_execution_results.json
```
Tùy chọn: `--mode {inprocess,external}`, `--db-path`, `--base-url`, `--allow-dirty-target`, `--timeout`, `--only`, `--limit`, `--no-observe`. Mã thoát 2 khi có `ExecutorError`.

## 7. Cách Phần 7 dùng kết quả
Với mỗi bước `SENT`, Result Validator tra expected qua `expected_ref`, so với `response.status_code`/`response.body` và `observed_state`; dùng `assertion_probes` cho các assertion trạng thái cuối; chỉ chấp nhận các case `COMPLETED` (hoặc xử lý riêng `PARTIAL`).
