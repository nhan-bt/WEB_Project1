# Thiết kế Fault Detection Report (Phần 8)

## 1. Vai trò
`fault_detection_report.py` là bước cuối của quy trình: tổng hợp `test_validation_results.json` (Phần 7) thành báo cáo lỗi logic nghiệp vụ dùng làm minh chứng nghiên cứu. Nó **chỉ đọc** file JSON; không gửi HTTP, không import backend/executor, không đọc database, không sửa kết quả Phần 7.

Nguyên tắc cốt lõi: **mỗi finding đến từ ít nhất một assertion FAIL có trong file Phần 7**. SKIPPED và INCONCLUSIVE được báo là "chưa kiểm chứng", không phải lỗi đã xác nhận. Mọi chỉ số được tính lại từ đầu vào, không hardcode.

## 2. Thành phần (trong một module; tách phân tích khỏi trình bày)
| Thành phần | Hàm chính | Nội dung |
|---|---|---|
| Validation Result Loader | `load_validation` | Kiểm tra schema (validator, schema_version, cấu trúc results/assertions), tính lại summary từ results và so khớp; FAIL không có assertion FAIL hoặc PASS có assertion FAIL → `ReportInputError` (không tạo báo cáo). ID trùng/thiếu được ghi vào `data_errors`. |
| References | `References` | Nạp `test_cases.json`, `efsm_model.json`, `security_rules.json`, và tùy chọn `test_paths.json`, `test_path_coverage.json`, `test_execution_results.json`. |
| Fault Extractor | `extract_faults` | Mỗi assertion FAIL → bản ghi: test case, path, transition (base transition cho negative), negative transition, rule, loại assertion, expected, actual, bước. |
| Evidence Collector | `collect_evidence` | Lấy từ execution result: request (method, url, body đã che), `status_code`, `expected_ref`, observed state trước/sau. Thiếu execution result → ghi `limited` và confidence LOW; không tạo bằng chứng giả. Assertion cấp test case được gắn với bước vi phạm đầu tiên (hoặc bước negative cuối). Khóa `customer_name/phone/address` luôn bị che. |
| Fault Classifier | `classify` | Xem mục 3. |
| Severity Analyzer | `assess_severity`, `assess_confidence` | Xem mục 4. |
| Fault Deduplicator | `fingerprint`, `deduplicate` | Gộp theo (loại sai lệch, negative transition/transition, tập rule, endpoint, trường vi phạm). Không dùng mã HTTP để gộp; không gộp hai lỗi chỉ vì cùng mã HTTP. |
| Coverage Analyzer | `analyze_coverage` | Xem mục 5. |
| Synthetic campaign | `run_synthetic_campaign` | Xem mục 6. |
| Report Generator | `build_report`, `render_html`, `render_markdown` | JSON, HTML tự chứa (không tài nguyên ngoài, hỗ trợ dark mode, escape mọi giá trị), Markdown. |

## 3. Phân loại hai nhóm nghiên cứu
Lưu cả mã chi tiết của Result Validator (`validator_category`) lẫn nhóm (`group`) và cơ sở phân loại (`classification_basis`). Thứ tự ưu tiên:
1. Theo nhãn Validator: `INVALID_STATE_TRANSITION`, `STOCK_INTEGRITY_VIOLATION`, `PAYMENT_LIFECYCLE_VIOLATION`, `RESOURCE_LIFECYCLE_VIOLATION` → **STATE_RESOURCE_LIFECYCLE**; `GUARD_VIOLATION` (negative về giới hạn bị chấp nhận) → **LIMITS_AUTHORIZATION**.
2. Với nhãn trung tính (`INVARIANT_VIOLATION`, `UNEXPECTED_SIDE_EFFECT`, `HTTP_STATUS_MISMATCH`, `RESPONSE_BODY_MISMATCH`): theo `negative_category` của test case (`INPUT_VALIDATION_VIOLATION`, `BUSINESS_CONSTRAINT_VIOLATION` → Limits; `INVALID_STATE_TRANSITION` → State).
3. Theo rule liên quan: nhóm các rule giới hạn dữ liệu (CART-*, ORDER-01/02/03, STOCK-01/03, PAYMENT-07) → Limits; các rule còn lại → State; rule thuộc cả hai nhóm → không kết luận.
4. Không đủ thông tin → **UNCLASSIFIED** kèm lý do.

BShop không có xác thực/phân quyền (BSR-AUTH-01 bị loại, đọc từ `excluded_rules`), nên nhóm Limits chỉ là giới hạn nghiệp vụ; không có finding nào được gắn nhãn ownership hay authentication. Ghi chú này được lấy từ dữ liệu và in trong báo cáo.

## 4. Severity và confidence
`severity` (ước tính) = tổng điểm các tiêu chí:
| Tiêu chí | Điểm |
|---|---|
| Thao tác lẽ ra bị từ chối nhưng được chấp nhận (`.REJECTED` FAIL) | +2 |
| Tác động: tiền/thanh toán 3, tồn kho 3, trạng thái đơn 2, invariant 2, giới hạn nghiệp vụ 1, tài nguyên 1, side effect 1, chỉ giao diện (HTTP/body) 0. Rule thuộc nhóm PAYMENT nâng tác động lên 3 (trừ lỗi chỉ giao diện) | 0–3 |
| Mức severity của rule liên quan trong `security_rules.json` (HIGH 2, MEDIUM 1) | 0–2 |
| Thay đổi trạng thái/dữ liệu được xác nhận qua observed state hoặc invariant | +1 |
| Tái hiện ở từ 2 test case trở lên | +1 |

Ngưỡng: ≥8 CRITICAL, 6–7 HIGH, 4–5 MEDIUM, 2–3 LOW, ≤1 INFO. Sai lệch chỉ ở mã HTTP/body không bao giờ vượt MEDIUM. Không có tiêu chí áp dụng (nhãn lạ, hoặc không rule và chỉ giao diện) → `UNDETERMINED`. Đây là ước tính theo tiêu chí tường minh, chưa xét khả năng khai thác ngoài thực tế (BShop là hệ thống mô phỏng, không có người dùng/phân quyền).

`confidence` đánh giá bằng chứng: HIGH khi có request, response và observed state trước/sau cho lỗi trạng thái/dữ liệu; MEDIUM khi chỉ có request/response hoặc lỗi giao diện; LOW khi chỉ có evidence của Phần 7.
`source_type`: `REAL_EXECUTION` hoặc `SYNTHETIC_INJECTION`.

## 5. Coverage
Tính từ execution result, test case và Result Validator, phân biệt:
- **Cấu trúc** và **khả thi**: lấy nguyên từ `test_path_coverage.json` (Phần 4).
- **Thực thi**: trạng thái đạt được (CART + trạng thái đơn quan sát), transition và negative transition đã gửi, cặp transition liên tiếp trong các bước `path` (so với số cặp possible theo mô hình), số test case đã/chưa thực thi.
- **Xác minh**: transition có assertion PASS và không có FAIL; rule có assertion PASS và không FAIL.
- Vùng chưa kiểm chứng: danh sách path chưa thực thi kèm lý do, cặp transition chưa thực thi, test case INCONCLUSIVE. Bao phủ 100% transition không đồng nghĩa 100% bảo mật nghiệp vụ.

## 6. Synthetic fault injection
Chạy ngay trong báo cáo (tắt bằng `--no-synthetic`), trên **bản sao** kết quả thực thi: chèn lỗi → Result Validator thật (`validate_case`) → Fault Reporter. Không sửa file gốc, không gọi API, không viết FAIL trực tiếp vào báo cáo. Kết quả nằm riêng trong `synthetic_fault_injection`, mọi finding mang `source_type = SYNTHETIC_INJECTION`, và không cộng vào số liệu thực tế.

| ID | Lỗi cài đặt | Test case chọn | Nhãn/nhóm kỳ vọng |
|---|---|---|---|
| FI01 | Hủy đơn đã PAID thành công (PAID→CANCELLED) | negative NT08 | INVALID_STATE_TRANSITION / State |
| FI02 | Đơn PAID được thanh toán lại thành công | NT07 | PAYMENT_LIFECYCLE_VIOLATION / State |
| FI03 | Hủy lần hai bị từ chối nhưng tồn kho hoàn lần nữa | NT09 | STOCK_INTEGRITY_VIOLATION / State |
| FI04 | API chấp nhận số lượng vượt tồn kho | NT02 | GUARD_VIOLATION / Limits |
| FI05 | Thanh toán thất bại nhưng đơn thành PAID | case kết thúc bằng T07 | INVALID_STATE_TRANSITION / State |
| FI06 | Mã từ chối hợp lệ nhưng dữ liệu vẫn đổi | NT01 | UNEXPECTED_SIDE_EFFECT |

Test case được chọn tự động theo `negative_transition_id`/transition cuối, không hardcode ID.

## 7. Định dạng đầu ra
`fault_detection_report.json`: `schema_version, project, report_type, reporter, source_type, inputs (sha256), summary{total_test_cases, passed, failed, skipped, inconclusive, assertions, total_findings, fault_metrics{failed_assertions, failed_test_cases, distinct_findings}, root_cause_clusters}, fault_categories, severity_counts, validator_categories, authorization_scope_note, findings[], coverage, data_errors, conclusion, synthetic_fault_injection, limitations, answers`. Mỗi finding: `finding_id, source_type, title, group, sub_class, classification_basis, validator_category, severity, severity_score, severity_criteria, severity_reason, confidence, transition_id, negative_transition_id, endpoint, violated_field, rules[], counts, test_case_ids, path_ids, occurrences[expected/actual], evidence`.
Báo cáo không chứa timestamp nên cùng đầu vào cho cùng đầu ra. Khi không có FAIL, kết luận là: "Không phát hiện sai lệch logic nghiệp vụ trong phạm vi các trường hợp đã thực thi và xác minh." và báo cáo không khẳng định hệ thống an toàn tuyệt đối.

## 8. Chạy
```
python fault_detection_report.py --validation test_validation_results.json --execution-results test_execution_results.json --test-cases test_cases.json --model efsm_model.json --rules security_rules.json --paths test_paths.json --coverage test_path_coverage.json --out-prefix fault_detection_report
```
Ghi `.json`, `.html`, `.md`. Mã thoát 0 khi tạo được báo cáo (kể cả khi có finding), 2 khi đầu vào không hợp lệ (không ghi file).
