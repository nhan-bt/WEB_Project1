# Báo cáo nghiệm thu Fault Detection Report (Phần 8)

Số liệu lấy từ lần chạy thật của `fault_detection_report.py` và từ pytest thật.

## 1. Kết quả báo cáo trên dữ liệu thực tế (REAL_EXECUTION)
| Chỉ số | Giá trị |
|---|---|
| Test case tổng hợp | 171 |
| PASS / FAIL / SKIPPED / INCONCLUSIVE | 122 / 0 / 49 / 0 |
| Assertion (tổng / đã đánh giá / FAIL / không thể đánh giá) | 6431 / 6431 / 0 / 0 |
| Finding thực tế | **0** |
| Finding synthetic (không tính vào số liệu thực tế) | 19, thuộc 6 kịch bản |

Kết luận trong báo cáo: "Không phát hiện sai lệch logic nghiệp vụ trong phạm vi các trường hợp đã thực thi và xác minh." Phạm vi: 122 test case đã thực thi; 49 path chưa thực thi (`NO_FEASIBLE_DATA_FOUND`) và chưa được kiểm chứng.

Coverage thực thi (tính lại từ dữ liệu): trạng thái 5/5; transition 10/10; negative transition 20/20; cặp transition 21/28 theo mô hình (tham chiếu Phần 4: 28 cấu trúc, 14 khả thi); rule được xác minh bằng PASS 26/26. Cặp chưa thực thi: T01>T10, T02>T10, T03>T10, T10>T02, T10>T03, T10>T04, T10>T10.

## 2. Kết quả fault injection (SYNTHETIC_INJECTION)
Lỗi được chèn vào bản sao kết quả thực thi, chạy qua Result Validator thật rồi Fault Reporter. Dữ liệu gốc không đổi.

| ID | Test case | Validator | Assertion FAIL | Finding | Nhóm | Severity |
|---|---|---|---|---|---|---|
| FI01 PAID→CANCELLED | TC110 (NT08) | FAIL | 5 | 5 | State & Resource Lifecycle | HIGH, MEDIUM, LOW |
| FI02 thanh toán lại | TC109 (NT07) | FAIL | 5 | 5 | State & Resource Lifecycle | HIGH, LOW |
| FI03 hoàn kho hai lần | TC111 (NT09) | FAIL | 4 | 4 | State & Resource Lifecycle | HIGH, MEDIUM |
| FI04 vượt tồn kho | TC104 (NT02) | FAIL | 3 | 3 | Limits & Authorization | MEDIUM, LOW |
| FI05 thanh toán thất bại → PAID | TC006 | FAIL | 1 | 1 | State & Resource Lifecycle | HIGH |
| FI06 từ chối nhưng đổi dữ liệu | TC103 (NT01) | FAIL | 1 | 1 | Limits & Authorization (UNEXPECTED_SIDE_EFFECT) | MEDIUM |

Kết quả: phát hiện 6/6, đúng nhãn Result Validator 6/6, đúng nhóm nghiên cứu 6/6. Mỗi finding có bằng chứng truy vết (request, status, observed state trước/sau, expected/actual, rule, transition). Đây là lỗi cài đặt có chủ đích, không phải lỗ hổng của BShop, và không chứng minh công cụ phát hiện được mọi loại lỗi. Mức severity là ước tính theo tiêu chí trong tài liệu thiết kế.

Một fault sinh nhiều finding (tối đa 5) vì có nhiều assertion độc lập (trạng thái, mã HTTP, invariant, dữ liệu) cùng vi phạm; báo cáo giữ riêng ba chỉ số: số assertion FAIL, số test case FAIL, số finding sau khi gộp.

## 3. Kết quả pytest (chạy thật)
- `python -m pytest tests/test_fault_detection_report.py`: **77 passed**.
- `python -m pytest` (toàn bộ): **736 passed** = 659 (Phần 1–7) + 77 (Phần 8). Không hồi quy.

Các nhóm test: loader/schema; tổng hợp trạng thái; trích xuất assertion FAIL; SKIPPED và INCONCLUSIVE không phải lỗi; phân loại Limits, State, UNCLASSIFIED; severity (điểm theo tiêu chí, HTTP không bao giờ CRITICAL, UNDETERMINED); truy vết rule/transition/expected-actual/bằng chứng; trùng lặp (gộp cùng lỗi qua nhiều test case, không gộp lỗi độc lập hoặc chỉ giống mã HTTP); coverage; JSON/HTML/Markdown hợp lệ và escape; không tạo finding khi không có FAIL; REAL so với SYNTHETIC; sáu kịch bản FI01–FI06; tái lập, không dùng HTTP/backend/DB, file nguồn không đổi.

Hai lỗi của chính báo cáo đã phát hiện và sửa khi viết test: (1) assertion cấp test case (A*, INV-*) chưa gắn bằng chứng execution; nay gắn với bước vi phạm đầu tiên hoặc bước negative cuối; (2) test tái hiện dùng cặp test case không cùng kết cấu, đã đổi sang cặp test case có cùng transition cuối.

## 4. An toàn
Các file Phần 1–7 (`efsm_model.json`, các module, `test_cases.json`, `test_execution_results.json`, `test_validation_results.json`, `app/`, `online_shop.db`) giống hệt bản trước (so sánh `cmp`). Báo cáo không chứa `customer_name`, `phone`, `address`.

## 5. Hạn chế
- 49 path chưa thực thi, 7 cặp transition chưa chạy; vùng này chưa được kiểm chứng.
- Kết quả phụ thuộc vào các assertion đã mô hình hóa và các GET API ở Phần 6; 40 assertion hậu điều kiện negative dùng `final_observed_state` thay probe độc lập.
- BShop không có xác thực/phân quyền nên không có phân loại lỗi ownership/authentication.
- Severity là ước tính theo tiêu chí tường minh, không phải đánh giá khai thác thực tế.
- Fault injection chỉ chứng minh khả năng phát hiện trên 6 mẫu lỗi.
- Không phát hiện lỗi trong phạm vi đã thực thi không đồng nghĩa hệ thống an toàn tuyệt đối.

## 6. Bốn câu hỏi của báo cáo
1. Đã kiểm thử: 122 test case (102 positive, 20 negative), 6431 assertion, 10/10 transition, 20/20 negative transition, 26/26 rule được xác minh bằng PASS.
2. Đạt 122, thất bại 0, chưa kiểm chứng 49 (SKIPPED), chưa kết luận 0.
3. Không có sai lệch thực tế.
4. Có: phát hiện 6/6 lỗi cài đặt, đúng nhãn 6/6, đúng nhóm nghiên cứu 6/6.
