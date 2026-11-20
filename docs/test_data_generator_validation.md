# Báo cáo kiểm thử Test Data Generator — Phần 2/2

Mọi số liệu dưới đây lấy từ lần chạy thật trên cây dự án này (Python 3.13). Không có số nào được ước lượng.

## 1. Lệnh nghiệm thu và kết quả

| Lệnh | Kết quả |
|---|---|
| `python -m py_compile test_data_generator.py` | Biên dịch thành công |
| `python -m pytest tests/test_test_data_generator.py -v` | **148 passed** (Phần 1: 82, Phần 2: 66) |
| `python -m pytest -v` | **498 passed** (432 ở baseline Phần 1 + 66 test mới), 0 failed |
| `python test_data_generator.py --model efsm_model.json --test-paths test_paths.json` | 171 path → 122 test case, ~1 giây |

Theo file: `test_api` 5, `test_efsm_backend_conformance` 33, `test_guard_evaluator` 174, `test_test_data_generator` 148, `test_test_path_generator` 138.

Các test Phần 2 **không** gọi API và không dùng database (có test kiểm tra `test_data_generator.py` không import `requests/httpx/urllib/socket/fastapi/sqlalchemy/sqlite3/app/subprocess`). Kết quả backend **chưa được thực thi**: mọi test case ở trạng thái `NOT_EXECUTED`.

## 2. Số liệu nghiệm thu

| Chỉ tiêu | Giá trị |
|---|---|
| Tổng số path đầu vào (`test_paths.json`) | 171 (151 positive, 20 negative) |
| Test case sinh ra | **122** (102 positive, 20 negative) |
| `READY` | 119 |
| `PARTIALLY_READY` | 3 (TC105 `!NT03`, TC106 `!NT04`, TC121 `!NT19`) |
| `BLOCKED` | 49 |
| `UNSUPPORTED` | 0 |
| Path negative đã chuyển | 20/20 (phủ đủ NT01…NT20) |
| Path không chuyển được (`unconverted_paths`) | 49 — tất cả `BLOCKED`/`NO_FEASIBLE_DATA_FOUND`, bằng chứng `SEARCH_EXHAUSTED`; trùng lặp: 0 |
| Path `INFEASIBLE` trong `test_paths.json` nay lập được dữ liệu | 70 (trên 119 path positive INFEASIBLE) |
| Test case có bước setup API | 75 (70 positive, 5 negative) |
| Bước fixture | 1 (TC106, NT04) |
| Rule bảo mật được liên kết | 26/26 rule có trong `test_paths.json` |
| Transition được phủ (ở vai trò bước path) | 10/10 |
| Negative transition được phủ | 20/20 |
| Cô lập vi phạm của negative | 18 `ISOLATED`, 0 `NOT_ISOLATED`, 2 `UNEVALUATED` (NT03, NT19) |
| Kỹ thuật sinh negative | 12 `STATE_OR_CONTEXT_VIOLATION`, 7 `EP_BVA_INPUT_VIOLATION`, 1 `FIXTURE_DATA_VIOLATION` |
| Dữ liệu theo loại (tổng các bước) | input 1101, derived 1878, backend_generated 238 |
| Invariant `VIOLATED` trên dữ liệu mô phỏng | 0 ở mọi checkpoint |
| Invariant có thể kiểm chứng trước khi chạy | **0**: mọi invariant `NOT_VERIFIED_UNTIL_EXECUTION` |
| Tái lập | Hai lần chạy cho `test_cases.json`/`test_data_coverage.json` giống hệt từng byte |

### 49 path bị chặn

Cả 49 đều có dạng `… T10 … T04 …`: T10 làm trống giỏ, sau đó T04 cần `cart_items_count > 0` mà path không có bước thêm hàng. Lý do ghi trong từng mục: `bước N của path (T04): GUARD_NOT_SATISFIED - UNSAT … (cart_items_count > 0 is false for the fixed values)`. `proof_scope` ghi rõ đây là tìm kiếm có giới hạn (catalog seed, tối đa 3 bước setup) chứ không phải chứng minh tổng quát. Generator **không sửa path**; việc loại các path này là quyết định của TestPathGenerator hoặc người dùng.

### Invariant không thể kiểm chứng trước khi thực thi

Cả 12 invariant (INV-01…INV-12). `SATISFIED` trong `simulated_status` chỉ nghĩa là đúng trên dữ liệu mô phỏng của EFSM (INV-01–05, 09, 11, 12) hoặc trên cấu trúc mô hình (INV-06, 07, 08, 10, `kind: model_structure`). Các số đếm checkpoint SATISFIED/SKIPPED nằm ở `test_data_coverage.json › invariants.per_invariant`.

## 3. Nhóm test Phần 2 (66 test)

| Nhóm | Số test | Nội dung chính |
|---|---|---|
| `TestApiMapping` | 5 | mapping khớp EFSM; phát hiện lệch path param/body; thiếu transition; capture không tồn tại |
| `TestShadowState` | 4 | đơn nhiều sản phẩm + hủy hoàn kho; `payment_count` chỉ đếm SUCCESS; sao chép độc lập; tên biến chiếu hợp lệ |
| `TestPositivePaths` | 10 | một/nhiều transition; **mọi bước positive được GuardEvaluator đánh giá lại** (hơn 300 bước); cập nhật ngữ cảnh T01→T04→T08; nhất quán số lượng T01 ↔ đơn T04; giá trị dẫn xuất; HTTP status từ EFSM; tham chiếu ký hiệu; path INFEASIBLE được lập lại; trạng thái `NOT_EXECUTED` |
| `TestNegativePaths` | 13 | bước âm tính đứng cuối và duy nhất; vi phạm được `check_negative_transition` xác nhận lại; tiền tố hợp lệ; cô lập một điều kiện; trạng thái sai; input validation; enum ngoài miền; business constraint; trạng thái/dữ liệu không đổi + không tác dụng phụ; fixture; cô lập chưa đánh giá được; `request_data` không có khóa thừa |
| `TestEdgeCases` | 15 | thiếu ngữ cảnh; không có dữ liệu hợp lệ (bounded, không phải chứng minh); hết ngân sách ≠ vô nghiệm; ngữ cảnh mâu thuẫn (tồn kho 0); action không mô phỏng được → `UNRESOLVED_CONTEXT`; transition lạ; thiếu API mapping; path dài 7 bước; self-loop cộng dồn số lượng; giỏ nhiều sản phẩm; guard áp cho từng dòng; nhiều lần thanh toán; ID do DB sinh không bao giờ là số cụ thể; 2 mutant của `ShadowState` bị phát hiện |
| `TestSelectionAndOutput` | 13 | số liệu tổng hợp nhất quán; path bị chặn có lý do; ID duy nhất; schema; `max_cases` ưu tiên negative / phủ transition mới; loại trùng; chữ ký; rule; coverage; invariant không bao giờ được ghi là đã kiểm chứng; không sửa đầu vào + tái lập; file đã bàn giao khớp output |
| `TestCoreExtensionAndScope` | 6 | `allow_off_domain`; `generated_guard_data.json` giữ nguyên từng byte; không import HTTP/DB; hash baseline không đổi; CLI hoạt động; CLI từ chối đầu vào sai |

## 4. Kiểm tra độ nhạy (mutation)

14 đột biến cố ý trong lớp path-aware, mỗi cái chạy lại `tests/test_test_data_generator.py` (bỏ qua duy nhất test so file đã bàn giao để không "giết" đột biến một cách tầm thường):

| Đột biến | Kết quả |
|---|---|
| bỏ đối chiếu mô phỏng ↔ action EFSM | KILLED |
| cô lập luôn là `ISOLATED` | KILLED |
| `order_id` dùng số cụ thể thay vì tham chiếu | KILLED |
| không loại trùng | KILLED |
| không dùng biến thể setup | KILLED |
| giá trị fixture sai | KILLED |
| negative không đổi đầu vào | KILLED |
| HTTP status negative sai | KILLED |
| bước âm tính đổi trạng thái | KILLED (sau khi thêm kiểm `expected.state_after`) |
| `payment_count` đếm cả payment FAILED | KILLED |
| bỏ kiểm guard từng dòng giỏ của T04 | KILLED (sau khi thêm test dòng vượt tồn kho) |
| coi hết ngân sách như hết kiểu tìm kiếm | KILLED |
| bỏ gợi ý khi giải đầu vào | KILLED |
| bỏ ưu tiên negative khi giới hạn `max_cases` | KILLED |

**14/14 bị phát hiện.** Lần chạy đầu 12/14; hai đột biến sống đã dẫn tới việc bổ sung hai test (kiểm `expected.state_after` của bước âm tính và `test_each_cart_line_must_satisfy_create_order_guard`). Không có kỳ vọng nào bị sửa để test qua.

Trong lúc viết test còn phát hiện hai lỗi schema của chính generator: `probe` của assertion negative ban đầu là chuỗi thay vì đối tượng, và bước negative thiếu `captures`. Cả hai được sửa trong mã để schema nhất quán cho Executor; kỳ vọng của test không bị đổi. Test đầu tiên về invariant giả định INV-06 "không bao giờ được đánh giá"; thực tế evaluator đánh giá `model_structure` trên cấu trúc mô hình, nên mã/báo cáo được làm rõ (`kind`, `simulated_basis`, `covered_by`) và test viết lại theo hành vi đúng.

## 5. Hồi quy và phạm vi

* Sau khi hoàn thành, các file baseline giống hệt bản bàn giao Phần 1 (so sánh byte và `tests/baseline_hashes.json`): `test_path_generator.py`, `test_paths.json`, `test_path_coverage.json`, `efsm_model.json`, `guard_evaluator.py`, `security_rules.json`, `test_path_context.bluemoon.json`, `generated_guard_data.json`, `test_data_context.bluemoon.json`; `app/` và `online_shop.db` không đổi.
* 82 test Phần 1 vẫn qua; `generated_guard_data.json` tái sinh giống hệt từng byte.
* Thay đổi duy nhất trong lõi: tham số `allow_off_domain` của `solve()` (mặc định tắt).

## 6. Ví dụ đầy đủ

### 6.1 Test case positive — TC102 (`T04>T08`, path P151)

`test_paths.json` đánh dấu P151 là `INFEASIBLE` (T04 trên giỏ trống). Generator thêm một bước setup API:

```json
{
  "test_case_id": "TC102", "source_path_id": "P151", "path_key": "T04>T08", "type": "positive",
  "readiness": "READY", "source_feasibility": "INFEASIBLE", "replanned": true,
  "steps": [
    { "step_no": 1, "role": "setup", "transition_id": "T01", "state_before": "CART", "state_after": "CART",
      "request": {"method": "POST", "path": "/api/cart/items", "body": {"product_id": 1, "quantity": 2}},
      "context_before": {"current_qty": 0, "product_exists": true, "quantity": 2, "stock": 10},
      "context_after": {"cart_items_count": 1, "current_qty": 2, "cart_item_exists": true},
      "expected": {"http_status": 200} },
    { "step_no": 2, "role": "path", "transition_id": "T04", "state_before": "CART", "state_after": "PENDING_CONFIRMATION",
      "request": {"method": "POST", "path": "/api/orders",
                  "body": {"customer_name": "NNNNNNNNNN", "phone": "9999999999", "address": "AAAAAAAAAAAAAAAAAAAA", "payment_method": "MOCK"}},
      "context_before": {"cart_items_count": 1, "ordered_quantity": 2, "stock": 10, "order_items": [{"quantity": 2, "unit_price": 150000}], "...": "..."},
      "context_after": {"stock_before": 10, "stock": 8, "order_total": 300000, "order_status": "PENDING_CONFIRMATION",
                        "cart_items_count": 0, "current_qty": 0, "cart_item_exists": false, "...": "..."},
      "captures": {"order_id": "$ref:steps.2.response.order.id"},
      "expected": {"http_status": 201, "response_checks": ["order.status == PENDING_CONFIRMATION", "order.total_amount == 300000"]} },
    { "step_no": 3, "role": "path", "transition_id": "T08", "state_before": "PENDING_CONFIRMATION", "state_after": "CANCELLED",
      "request": {"method": "POST", "path": "/api/orders/{order_id}/cancel", "path_params": {"order_id": "$ref:steps.2.response.order.id"}},
      "context_before": {"order_status": "PENDING_CONFIRMATION", "ordered_quantity": 2, "stock": 8, "cancel_count": 0, "stock_restoration_count": 0},
      "context_after": {"stock": 10, "stock_after": 10, "order_status": "CANCELLED", "cancel_count": 1, "stock_restoration_count": 1},
      "expected": {"http_status": 200} }
  ],
  "expected_final_data": {"cart_items": [], "product_stock": {"1": 10, "2": 5, "3": 3},
                          "orders": [{"order_ref": "$ref:steps.2.response.order.id", "status": "CANCELLED", "total_amount": 300000}],
                          "payments": []}
}
```

(Ví dụ đã rút gọn; bản đầy đủ nằm trong `test_cases.json`, mục `TC102`.)

### 6.2 Test case negative — TC110 (`T01>T04>T05>T06>!NT08`, path N008)

Hủy một đơn đã thanh toán (BSR-CANCEL-01/02). Tiền tố T01→T04→T05→T06 đưa hệ thống tới `PAID`, rồi bước âm tính:

```json
{
  "test_case_id": "TC110", "source_path_id": "N008", "type": "negative", "readiness": "READY",
  "steps": [ "...4 bước tiền tố đều role=path, mỗi bước GUARD_SATISFIED...",
    { "step_no": 5, "role": "negative", "negative_transition_id": "NT08", "base_transition_id": "T08",
      "state_before": "PAID", "state_after": "PAID",
      "request": {"method": "POST", "path": "/api/orders/{order_id}/cancel", "path_params": {"order_id": "$ref:steps.2.response.order.id"}, "body": {}},
      "request_data": {},
      "context_before": {"order_status": "PAID", "stock": 8, "ordered_quantity": 2, "cancel_count": 0, "stock_restoration_count": 0},
      "violation": {"invalid_condition": "order_status == PAID", "technique": "STATE_OR_CONTEXT_VIOLATION",
                    "changed_variables": [], "evaluator_status": "NEGATIVE_APPLICABLE",
                    "isolation": {"status": "ISOLATED", "failed_conditions": ["order_status in [\"PENDING_CONFIRMATION\", \"CONFIRMED\"]"]}},
      "expected": {"outcome": "REJECT", "http_status": 409, "http_status_source": "efsm_model.negative_transitions[NT08].expected.http_status",
                   "state_after": "PAID", "state_unchanged": true, "data_unchanged": true} } ],
  "expected_final_state": "PAID",
  "assertions": ["GET /api/cart", "GET /api/products (stock 1 = 8)", "GET /api/payments (đúng 1 bản ghi SUCCESS)", "GET /api/orders/{order_id} (status PAID)"]
}
```

Đây là vi phạm "do trạng thái": dữ liệu đầu vào không bị đổi (đơn đã `PAID` do tiền tố). Ví dụ vi phạm "do đầu vào" là TC104 (`!NT02`): `POST /api/cart/items` với `quantity = 11` khi `stock = 10` (BVA: `stock + 1`), `ISOLATED` vì chỉ `current_qty + quantity <= stock` sai.

## 7. Điều chưa được giải quyết / không được khẳng định

* Không có test case nào được thực thi trên backend; mọi giá trị kỳ vọng ngoài HTTP status đến từ mô phỏng.
* NT03, NT19: cô lập vi phạm chưa đánh giá được (`UNEVALUATED`) → `PARTIALLY_READY`.
* NT04: cần fixture đổi tồn kho trong DB; chỉ được mô tả.
* 49 path positive `BLOCKED` (T10 trước T04).
* Backend không có API reset: mọi test case cần fixture dọn DB trước khi chạy (`cleanup`).
* `mapping.fixtures` mới có biến `stock`.
