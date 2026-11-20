# Báo cáo phát hiện sai lệch logic nghiệp vụ – BShop

Loại nguồn: **REAL_EXECUTION**. Báo cáo được tính lại từ `test_validation_results.json`.

## 1. Kết luận

> Không phát hiện sai lệch logic nghiệp vụ trong phạm vi các trường hợp đã thực thi và xác minh.

Phạm vi: 122 test case đã thực thi; 49 chưa thực thi, 0 chưa kết luận được. PASS không có nghĩa hệ thống an toàn tuyệt đối.

## 2. Thống kê test case

| Trạng thái | Số lượng |
|---|---|
| Tổng | 171 |
| PASS | 122 |
| FAIL | 0 |
| SKIPPED (chưa kiểm chứng) | 49 |
| INCONCLUSIVE | 0 |

## 3. Thống kê assertion

Tổng 6431; đã đánh giá 6431; PASS 6431; FAIL 0; không thể đánh giá 0.

Ba chỉ số lỗi: 0 assertion FAIL, 0 test case FAIL, 0 finding sau khi gộp trùng lặp.

## 4. Độ bao phủ

| Chỉ số | Thực thi |
|---|---|
| Trạng thái | 5/5 |
| Transition | 10/10 |
| Negative transition | 20/20 |
| Cặp transition (theo mô hình) | 21/28 |
| Rule được xác minh (PASS) | 26/26 |

Tham chiếu cấu trúc (Phần 4): cặp transition cấu trúc 28/28, khả thi 14. Cấu trúc ≠ khả thi ≠ thực thi.

## 5. Phân loại lỗi

| Nhóm | Số finding |
|---|---|
| LIMITS_AUTHORIZATION | 0 |
| STATE_RESOURCE_LIFECYCLE | 0 |
| UNCLASSIFIED | 0 |

| Severity | Số finding |
|---|---|
| CRITICAL | 0 |
| HIGH | 0 |
| MEDIUM | 0 |
| LOW | 0 |
| INFO | 0 |
| UNDETERMINED | 0 |

_BShop không triển khai xác thực/phân quyền theo người dùng (quy tắc BSR-AUTH-01 bị loại khỏi phạm vi: không áp dụng). Vì vậy nhóm LIMITS_AUTHORIZATION chỉ phản ánh giới hạn và ràng buộc nghiệp vụ; không có finding nào được gắn nhãn lỗi ownership hay authentication._

## 6. Findings thực tế

Không phát hiện sai lệch logic nghiệp vụ trong phạm vi các trường hợp đã thực thi và xác minh.

## 7. Fault injection (SYNTHETIC_INJECTION)

_Lỗi cài đặt có chủ đích vào bản sao dữ liệu thực thi; KHÔNG phải lỗ hổng của BShop._

| ID | Kịch bản | Test case | Phát hiện | Đúng nhãn | Đúng nhóm | Severity |
|---|---|---|---|---|---|---|
| FI01 | Hủy đơn đã thanh toán (PAID → CANCELLED) | TC110 | có | có | có | HIGH, MEDIUM |
| FI02 | Thanh toán lại đơn đã PAID thành công | TC109 | có | có | có | HIGH |
| FI03 | Hủy hai lần làm tồn kho được hoàn hai lần | TC111 | có | có | có | HIGH |
| FI04 | Chấp nhận số lượng vượt tồn kho | TC104 | có | có | có | MEDIUM |
| FI05 | Thanh toán thất bại nhưng đơn chuyển PAID | TC006 | có | có | có | HIGH |
| FI06 | Mã từ chối hợp lệ nhưng dữ liệu vẫn bị thay đổi | TC103 | có | có | có | MEDIUM |

Tổng: phát hiện 6/6, đúng nhãn 6, đúng nhóm 6.

## 8. Chưa được kiểm chứng

- P029 `T01>T10>T04>T05>T06`: NO_FEASIBLE_DATA_FOUND
- P030 `T01>T10>T04>T05>T07>T06`: NO_FEASIBLE_DATA_FOUND
- P031 `T01>T10>T04>T05>T07>T07>T06`: NO_FEASIBLE_DATA_FOUND
- P032 `T01>T10>T04>T05>T07>T07>T09`: NO_FEASIBLE_DATA_FOUND
- P033 `T01>T10>T04>T05>T07>T09`: NO_FEASIBLE_DATA_FOUND
- P034 `T01>T10>T04>T05>T09`: NO_FEASIBLE_DATA_FOUND
- P035 `T01>T10>T04>T08`: NO_FEASIBLE_DATA_FOUND
- P061 `T02>T10>T04>T05>T06`: NO_FEASIBLE_DATA_FOUND
- P062 `T02>T10>T04>T05>T07>T06`: NO_FEASIBLE_DATA_FOUND
- P063 `T02>T10>T04>T05>T07>T07>T06`: NO_FEASIBLE_DATA_FOUND
- P064 `T02>T10>T04>T05>T07>T07>T09`: NO_FEASIBLE_DATA_FOUND
- P065 `T02>T10>T04>T05>T07>T09`: NO_FEASIBLE_DATA_FOUND
- P066 `T02>T10>T04>T05>T09`: NO_FEASIBLE_DATA_FOUND
- P067 `T02>T10>T04>T08`: NO_FEASIBLE_DATA_FOUND
- P096 `T03>T10>T04>T05>T06`: NO_FEASIBLE_DATA_FOUND
- P097 `T03>T10>T04>T05>T07>T06`: NO_FEASIBLE_DATA_FOUND
- P098 `T03>T10>T04>T05>T07>T07>T06`: NO_FEASIBLE_DATA_FOUND
- P099 `T03>T10>T04>T05>T07>T07>T09`: NO_FEASIBLE_DATA_FOUND
- P100 `T03>T10>T04>T05>T07>T09`: NO_FEASIBLE_DATA_FOUND
- P101 `T03>T10>T04>T05>T09`: NO_FEASIBLE_DATA_FOUND
- P102 `T03>T10>T04>T08`: NO_FEASIBLE_DATA_FOUND
- P117 `T10>T02>T04>T05>T06`: NO_FEASIBLE_DATA_FOUND
- P118 `T10>T02>T04>T05>T07>T06`: NO_FEASIBLE_DATA_FOUND
- P119 `T10>T02>T04>T05>T07>T07>T06`: NO_FEASIBLE_DATA_FOUND
- P120 `T10>T02>T04>T05>T07>T07>T09`: NO_FEASIBLE_DATA_FOUND
- P121 `T10>T02>T04>T05>T07>T09`: NO_FEASIBLE_DATA_FOUND
- P122 `T10>T02>T04>T05>T09`: NO_FEASIBLE_DATA_FOUND
- P123 `T10>T02>T04>T08`: NO_FEASIBLE_DATA_FOUND
- P124 `T10>T03>T04>T05>T06`: NO_FEASIBLE_DATA_FOUND
- P125 `T10>T03>T04>T05>T07>T06`: NO_FEASIBLE_DATA_FOUND
- P126 `T10>T03>T04>T05>T07>T07>T06`: NO_FEASIBLE_DATA_FOUND
- P127 `T10>T03>T04>T05>T07>T07>T09`: NO_FEASIBLE_DATA_FOUND
- P128 `T10>T03>T04>T05>T07>T09`: NO_FEASIBLE_DATA_FOUND
- P129 `T10>T03>T04>T05>T09`: NO_FEASIBLE_DATA_FOUND
- P130 `T10>T03>T04>T08`: NO_FEASIBLE_DATA_FOUND
- P131 `T10>T10>T04>T05>T06`: NO_FEASIBLE_DATA_FOUND
- P132 `T10>T10>T04>T05>T07>T06`: NO_FEASIBLE_DATA_FOUND
- P133 `T10>T10>T04>T05>T07>T07>T06`: NO_FEASIBLE_DATA_FOUND
- P134 `T10>T10>T04>T05>T07>T07>T09`: NO_FEASIBLE_DATA_FOUND
- P135 `T10>T10>T04>T05>T07>T09`: NO_FEASIBLE_DATA_FOUND
- P136 `T10>T10>T04>T05>T09`: NO_FEASIBLE_DATA_FOUND
- P137 `T10>T10>T04>T08`: NO_FEASIBLE_DATA_FOUND
- P138 `T10>T04>T05>T06`: NO_FEASIBLE_DATA_FOUND
- P139 `T10>T04>T05>T07>T06`: NO_FEASIBLE_DATA_FOUND
- P140 `T10>T04>T05>T07>T07>T06`: NO_FEASIBLE_DATA_FOUND
- P141 `T10>T04>T05>T07>T07>T09`: NO_FEASIBLE_DATA_FOUND
- P142 `T10>T04>T05>T07>T09`: NO_FEASIBLE_DATA_FOUND
- P143 `T10>T04>T05>T09`: NO_FEASIBLE_DATA_FOUND
- P144 `T10>T04>T08`: NO_FEASIBLE_DATA_FOUND

## 9. Giới hạn

- 49 test case/path chưa được thực thi (NO_FEASIBLE_DATA_FOUND: 49); khu vực này chưa được kiểm chứng.
- 0 test case INCONCLUSIVE (chưa kết luận).
- Kết quả chỉ phản ánh những gì quan sát được qua các GET API của Phần 6 và các assertion đã mô hình hóa; không đọc database trực tiếp.
- 40 assertion hậu điều kiện negative dùng final_observed_state thay cho probe độc lập (probe thiếu path param ở Phần 6).
- BShop không triển khai xác thực/phân quyền theo người dùng (quy tắc BSR-AUTH-01 bị loại khỏi phạm vi: không áp dụng). Vì vậy nhóm LIMITS_AUTHORIZATION chỉ phản ánh giới hạn và ràng buộc nghiệp vụ; không có finding nào được gắn nhãn lỗi ownership hay authentication.
- Độ bao phủ cấu trúc, khả thi và thực thi là ba khái niệm khác nhau; bao phủ transition không đồng nghĩa bảo mật nghiệp vụ đầy đủ.
- Fault injection chỉ chứng minh khả năng phát hiện của công cụ trên các mẫu lỗi đã chọn; không phải lỗ hổng thực tế của BShop và không chứng minh phát hiện được mọi lỗi.
- Các cặp transition chưa thực thi: T01>T10, T02>T10, T03>T10, T10>T02, T10>T03, T10>T04, T10>T10.

## 10. Bốn câu hỏi

- **1_da_kiem_thu_gi**: 122 test case đã thực thi (NEGATIVE: 20, POSITIVE: 102); 6431 assertion; transition thực thi 10/10, negative transition 20/20, rule được xác minh bằng PASS 26/26.
- **2_dat_that_bai_chua_kiem_chung**: PASS 122, FAIL 0, SKIPPED 49 (chưa kiểm chứng), INCONCLUSIVE 0 (chưa kết luận).
- **3_sai_lech_va_bang_chung**: Không có finding thực tế.
- **4_phat_hien_loi_cai_dat**: Phát hiện 6/6 lỗi cài đặt; đúng nhãn 6/6; đúng nhóm nghiên cứu 6/6 (source_type=SYNTHETIC_INJECTION, không phải lỗi thực tế).
