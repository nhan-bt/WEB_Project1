# Báo cáo nghiệm thu Result Validator (Phần 7)

Mọi số liệu lấy từ lần chạy thật của `result_validator.py` trên `test_cases.json` + `test_execution_results.json` và từ pytest thật.

## 1. Kết quả validation trên dữ liệu thật
| Chỉ số | Giá trị |
|---|---|
| Tổng số mục | 171 |
| PASS | 122 (102 positive, 20 negative) |
| FAIL | 0 |
| SKIPPED | 49 (path không chuyển đổi được, `NO_FEASIBLE_DATA_FOUND`) |
| INCONCLUSIVE | 0 |
| Assertion tổng / đã đánh giá / PASS / FAIL / không thể đánh giá | 6431 / 6431 / 6431 / 0 / 0 |
| Lỗi dữ liệu (`data_errors`) | 0 |

Phân bố assertion (cộng đúng 6431): HTTP 693; trạng thái đơn 693; dữ liệu theo miền (cart, stock, orders, payments) 2.772; body response 993; bước negative bị từ chối 20; assertion `A*` của test case 554; invariant 706 (runtime: INV-01 115, INV-02 108, INV-03 108, INV-04 45, INV-05 59, INV-09 108, INV-11 108, INV-12 45; cấu trúc: INV-06 1, INV-07 3, INV-08 3, INV-10 3).
Có 80 assertion hậu điều kiện negative (4 assertion `A*` ở mỗi trong 20 case negative): 40 dùng probe, 40 (probe sản phẩm/đơn hàng thiếu path param nên `NOT_SENT` ở Phần 6) dùng `final_observed_state`, ghi rõ trong `evidence.actual_from`.

**Diễn giải:** với 122 test case đã chạy, hành vi quan sát được của backend khớp đặc tả (EFSM, test case, invariant đã mô hình hóa). Không phát hiện sai lệch trong phạm vi này; đây là kết quả của dữ liệu thật, không phải mục tiêu đặt trước. Kết luận chỉ áp dụng cho 122 case đã thực thi và các assertion nêu trên.

## 2. Độ nhạy: synthetic fault fixtures
Validator phải bắt được lỗi cài đặt vào **bản sao** kết quả thực thi (file gốc không đổi). Các lỗi đã kiểm thử và đều được phát hiện (FAIL kèm expected/actual/evidence):
- PAID → CANCELLED sau negative NT08 (HTTP vẫn 409); NT08 được chấp nhận trái phép (409 → 200 + CANCELLED).
- HTTP đúng nhưng trạng thái sai; HTTP sai (404) nhưng trạng thái đúng.
- Thanh toán thành công nhưng đơn không PAID; thanh toán thất bại nhưng đơn thành PAID; đơn PAID không có payment SUCCESS.
- Hủy đơn không hoàn tồn kho; hủy lần hai (NT09) hoàn tồn kho lần nữa (INV-05).
- Tồn kho sai giá trị; tồn kho âm (INV-11); thanh toán thành công trùng lặp (INV-09); tổng tiền đơn bị sửa (INV-03).
- Side effect trên cart/orders sau yêu cầu bị từ chối; body response sai (tổng tiền, `payment.order_id`).
Thiếu observed state, kết quả thực thi lỗi, assertion không hỗ trợ, mâu thuẫn đặc tả, nguồn không nhất quán → INCONCLUSIVE; test case SKIPPED → SKIPPED; thiếu `test_case_id` → ghi nhận lỗi dữ liệu.

## 3. Kết quả pytest (chạy thật)
- `python -m pytest tests/test_result_validator.py`: **73 passed**.
- `python -m pytest` (toàn bộ): **659 passed** = 586 (Phần 1–6) + 73 (Phần 7). Không hồi quy.

Một lỗi của chính validator đã phát hiện và sửa trong lúc phát triển: lần chạy đầu báo 130 FAIL ở `payment.order_id` vì chưa giải tham chiếu `$ref` trong giá trị mong đợi; sau khi sửa, kết quả như bảng trên. Không sửa dữ liệu hay backend để đổi kết quả.

## 4. An toàn
`efsm_model.json`, `guard_evaluator.py`, `test_path_generator.py`, `test_data_generator.py`, `test_cases.json`, `api_test_mapping.json`, `api_test_executor.py`, `test_execution_results.json`, `app/`, `online_shop.db` giống hệt bản trước khi làm Phần 7 (so sánh `cmp`). Test xác nhận validator không import HTTP/executor/backend/DB và không chứa dữ liệu nhạy cảm trong đầu ra.

## 5. Hạn chế
- Chỉ kiểm chứng những gì quan sát được qua GET API ở Phần 6; không đọc database trực tiếp.
- 49 path chưa chạy nên chưa được kiểm chứng.
- Invariant chỉ đánh giá những invariant có trong `efsm_model.json` và trong `invariant_assertions` của test case; không thêm luật mới. INV-06/07/08/10 là cấu trúc mô hình, chỉ kiểm chứng qua negative transition tương ứng.
- Hậu điều kiện negative cho sản phẩm/đơn hàng dùng `final_observed_state` thay probe (probe Phần 6 thiếu path param); nên cân nhắc cải thiện ở Phần 6 nếu cần probe độc lập.
- Expected phụ thuộc test case/EFSM của Phần 2–5; lỗi ở đặc tả sẽ phản ánh vào kết quả (được nhận diện ở mức mâu thuẫn nguồn).
- Phần 7 chưa gán mức độ nghiêm trọng hay kết luận lỗ hổng.

## 6. Sẵn sàng cho Phần 8
Mỗi assertion có `test_case_id`, `path_id`, `transition_id`, `rule_refs`, `expected`, `actual`, `evidence`; tổng hợp theo `failures_by_category`. Hiện không có FAIL thật nên Phần 8 sẽ báo cáo "không phát hiện sai lệch" cho phạm vi 122 case; các fixture lỗi trong test chứng minh đường phát hiện hoạt động.
