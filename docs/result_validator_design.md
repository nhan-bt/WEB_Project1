# Thiết kế Result Validator (Phần 7)

## 1. Vai trò
`result_validator.py` đối chiếu kết quả thực thi của Phần 6 (`test_execution_results.json`) với hành vi mong đợi và phân loại từng test case thành **PASS / FAIL / SKIPPED / INCONCLUSIVE**, kèm bằng chứng expected/actual cho Phần 8 (Fault Detection Report).

Validator chỉ **đọc** bốn file JSON: `test_cases.json`, `test_execution_results.json`, `efsm_model.json`, `security_rules.json`. Nó không gửi HTTP, không import executor hay backend, không đọc database. Phần 8 dùng `test_validation_results.json` mà không cần chạy lại API.

## 2. Thành phần
| Thành phần | Trách nhiệm |
|---|---|
| `Inputs` (Result Loader) | Nạp và kiểm tra cấu trúc 4 file (schema_version 1.0), lập chỉ mục transition/negative/invariant/rule, lưu sha256 nguồn. |
| Kiểm tra nhất quán | `test_case_id`, `path_id` = `source_path_id`, `test_type`, `negative_transition_id`, danh sách `step_no`/`transition_id`. Lệch → `INCONSISTENT_SOURCES` (INCONCLUSIVE). Thiếu ID → `MISSING_TEST_CASE_ID`. Kết quả không có test case → `UNKNOWN_TEST_CASE`; trùng → `DUPLICATE_RESULT`; sha256 nguồn không khớp → `SOURCE_HASH_MISMATCH` (mọi case INCONCLUSIVE). |
| Expected Result Resolver (`resolve_expected`) | Lấy expected từ bước của test case; đối chiếu với EFSM (`transitions[].expected_http_status`, `negative_transitions[].expected`) và `security_rules[].violation_expected`. Mâu thuẫn → `SPEC_CONFLICT`, assertion INCONCLUSIVE, không chọn bên nào. |
| HTTP Response Validator | So `status_code`; mỗi `response_checks` (`response.order.status`, `payment.result/amount/order_id`, `order.total_amount/items`) một assertion. Giá trị `$ref` được giải bằng ID thật từ response của executor. Toán tử khác `==` → `UNSUPPORTED_ASSERTION`. |
| State Transition Validator | Trạng thái đơn quan sát sau mỗi bước so với `data_after`/`state_after` của test case (T04 → PENDING_CONFIRMATION, T05 → CONFIRMED, T06 → PAID, T07 → CONFIRMED, T08/T09 → CANCELLED). CART chỉ là trạng thái trừu tượng, không so với `orders.status`. |
| Data Integrity Validator | Mỗi bước so cart, stock, orders (tổng tiền/dòng hàng/số đơn), payments với `data_after`. Thay đổi hợp lệ do transition tạo ra nằm sẵn trong `data_after`, nên không yêu cầu database bất biến sau positive. |
| Negative Transition Validator | Mỗi bước negative: (1) bị từ chối (không 2xx), (2) đúng mã HTTP, (3) trạng thái đơn không đổi, (4) cart/stock/orders/payments không đổi so với `observed_state` của bước liền trước. |
| Invariant Validator | Đánh giá runtime INV-01, 02, 03, 04, 05, 09, 11, 12 trên mọi trạng thái quan sát. INV-06/07/08/10 là thuộc tính cấu trúc mô hình: chỉ có assertion ở test case chứa negative transition kiểm chứng chúng (NT08; NT05/NT10/NT20; NT06/NT07/NT11; NT09/NT10/NT11), lấy kết quả từ việc bị từ chối + trạng thái không đổi (không phụ thuộc mã HTTP). |
| Assertion cấp test case | Các assertion `A1…` của Phần 5 được đánh giá bằng `assertion_probes`. Hậu điều kiện negative (`check`) so với ảnh chụp trước bước negative; probe thiếu path param (`NOT_SENT`) thì dùng `final_observed_state` (cũng lấy từ GET) và ghi rõ trong `evidence.actual_from`. |

## 3. Quy tắc phân loại
- **SKIPPED**: executor trả SKIPPED (gồm 49 path `NO_FEASIBLE_DATA_FOUND`).
- **INCONCLUSIVE**: không có kết quả thực thi; `execution_status` khác COMPLETED (EXECUTION_ERROR/PARTIAL); có bước không gửi; môi trường lệch; nguồn không nhất quán; thiếu/ lỗi `observed_state`; assertion không hỗ trợ; `SPEC_CONFLICT`; tiền đề guard không thỏa trong test case (`SPEC_OR_TESTDATA_ISSUE`) — và không có FAIL nào.
- **FAIL**: có ít nhất một assertion bị vi phạm (dựa trên dữ liệu quan sát đầy đủ). FAIL ưu tiên hơn INCONCLUSIVE.
- **PASS**: có assertion, tất cả được đánh giá và thỏa.
- HTTP 4xx không tự là FAIL, 2xx không tự là PASS; `COMPLETED` không bao giờ được coi là PASS.
- Không có dữ liệu nào bị suy ra từ HTTP status; trạng thái luôn lấy từ `observed_state`.

## 4. Nhãn sai lệch (sơ bộ, không phải mức độ nghiêm trọng)
`HTTP_STATUS_MISMATCH`, `RESPONSE_BODY_MISMATCH`, `INVALID_STATE_TRANSITION`, `GUARD_VIOLATION` (negative được chấp nhận trong khi nhóm lỗi không phải trạng thái), `INVARIANT_VIOLATION`, `UNEXPECTED_SIDE_EFFECT`, `STOCK_INTEGRITY_VIOLATION`, `PAYMENT_LIFECYCLE_VIOLATION`, `RESOURCE_LIFECYCLE_VIOLATION`. Loại không kết luận được: `SPEC_CONFLICT`, `MISSING_OBSERVED_DATA`, `UNSUPPORTED_ASSERTION`, `SPEC_OR_TESTDATA_ISSUE`.

## 5. Định dạng `test_validation_results.json`
`schema_version`, `validator`, `scope`, `sources` (sha256), `summary{total, passed, failed, skipped, inconclusive, by_test_type, assertions{total, evaluated, passed, failed, not_evaluable}, failures_by_category, data_errors}`, `data_errors[]`, `results[]`.
Mỗi kết quả: `test_case_id, path_id, path_key, test_type, negative_transition_id, readiness, execution_status, validation_status, reasons[], data_errors[], notes[], assertion_counts, assertions[]`.
Mỗi assertion: `assertion_id, check, transition_id, step_no, category, status, expected, actual, expected_source, rule_refs[], evidence{execution_step, expected_ref, request, http_status, observed_ref, …}`.
Truy vết cho Phần 8: Test Case → Path → Transition → Rule → Expected → Actual → Evidence. Không sao chép response body, không có `customer_name/phone/address`. Không có timestamp nên cùng đầu vào cho cùng đầu ra.

## 6. Chạy
```
python result_validator.py --test-cases test_cases.json --execution-results test_execution_results.json --model efsm_model.json --rules security_rules.json --output test_validation_results.json
```
Mã thoát: 0 (kể cả khi có FAIL, vì FAIL là kết quả kiểm thử), 1 khi dùng `--fail-on-fail` và có FAIL, 2 khi đầu vào lỗi.
