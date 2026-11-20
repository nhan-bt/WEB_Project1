# Báo cáo chuẩn hóa EFSM và GuardEvaluator — BlueMoon

## 1. Tóm tắt

| Hạng mục | Kết quả |
|---|---|
| Backend (`app/`), UI, schema DB, `security_rules.json`, `tests/test_api.py` | **Không thay đổi** (đã so sánh với file trong zip) |
| `online_shop.db` (DB demo) | Không bị chạm tới (checksum trước/sau giống nhau); mọi test API dùng `test_online_shop.db` |
| `efsm_model.json` | v1 → **v2**: giữ nguyên mọi ID `T01–T09`, `INV-01–10`, `NT01–12`; thêm `T10`, `INV-11/12`, `NT13–NT20` |
| `guard_evaluator.py` | Giữ kiến trúc và API công khai; thêm status, action có cấu trúc, invariant có scope |
| Kết quả test | **212 passed** (174 unit evaluator + 33 conformance backend + 5 `test_api.py` gốc) |
| `IMPLEMENTATION_DEFECT` | **Không phát hiện** (không sửa backend) |

## 2. File đã tạo / thay đổi

| File | Trạng thái | Vai trò |
|---|---|---|
| `efsm_model.json` | sửa (v2) | Mô hình chuẩn hóa, đối chiếu với backend |
| `guard_evaluator.py` | sửa | Evaluator |
| `tests/test_guard_evaluator.py` | mới | 174 unit test (không API, không DB) |
| `tests/test_efsm_backend_conformance.py` | mới | 33 integration test chạy API thật trên DB test riêng; kỳ vọng đọc từ mô hình |
| `conftest.py` | mới (rỗng, chỉ docstring) | Để `pytest` thường cũng import được `guard_evaluator` và `app` |
| `docs/efsm_backend_mapping.md` | mới | Bảng đối chiếu, truy vết, danh sách sai lệch |
| `docs/efsm_validation_report.md` | mới | Báo cáo này |

`workflow.json` trong zip là file **rỗng (0 byte)**; không dùng. Thư mục `IT3180/` chỉ chứa `.git`.

## 3. Thiết kế

### Guard (giữ DSL hiện có)
`all | any | not | {var, op, value|value_var} | {expr}` với toán tử `== != > >= < <= in not_in exists not_empty`.
`expr` được phân tích bằng `ast.parse` rồi duyệt theo **whitelist** (so sánh, and/or/not, `+ - * /`, hằng số, biến, enum, danh sách, hàm `SUM_PRODUCT`).
Không có `eval`/`exec` (có test AST bảo vệ). Quy tắc kiểu: toán tử thứ tự (`> >= < <=`) chỉ nhận số và bị chặn **ngay khi nạp mô hình** nếu dùng với biến chuỗi hay literal chuỗi.

### Action (mới: có cấu trúc)
`assign {var, expr|value, when?}` được mô phỏng; `effect {description}` là tác động lên backend/DB và **không** được mô phỏng
(`TransitionOutcome.backend_effects`, `simulated=True` luôn bật). Chuỗi action kiểu cũ vẫn được chấp nhận (`x := expr` → assign nếu parse được, còn lại → effect).

### Invariant
Mỗi invariant có `scope` (CART/ORDER/PAYMENT/GLOBAL), `applies_in_states`, `applies_when`, và một trong:
`check` (cùng DSL guard) · `kind: model_structure` · `evaluable: false` + `unsupported_reason`.
Một invariant chỉ có văn bản mà không khai báo gì thêm bị **từ chối khi nạp** (strict) — không thể "trông như kiểm tra được" mà thực ra không.

| v1 | v2 |
|---|---|
| INV-03 `SUM(...)` | `order_total == SUM_PRODUCT(order_items,'quantity','unit_price')`; runtime context cung cấp `order_items` |
| INV-04 `exists payment(result==SUCCESS)` | `payment_count >= 1` khi `PAID`; `payment_count` = số payment SUCCESS (biến dẫn xuất, đã khai báo rõ) |
| INV-09 `successful_payment_count_per_order` | `payment_count <= 1` |
| INV-06/07/08/10 (thuộc tính của sự kiện) | `model_structure`: kiểm tra mô hình không có transition vi phạm; backend được kiểm chứng bằng NT05–NT11, NT20 |

Kết quả invariant tách bạch: `SATISFIED`, `VIOLATED`, `SKIPPED` (kèm `NOT_APPLICABLE_STATE` / `NOT_APPLICABLE_CONDITION` / `MISSING_VARIABLE`), `UNSUPPORTED`, `ERROR`.
Thiếu biến **không bao giờ** thành `VIOLATED`; `UNSUPPORTED` **không bao giờ** thành `SATISFIED`; `report.ok` không đồng nghĩa mọi thứ đã được kiểm tra — xem `report.fully_evaluated` và `summary()`.

### Status của guard / transition
`GUARD_SATISFIED, GUARD_NOT_SATISFIED, STATE_MISMATCH, MISSING_VARIABLE, TYPE_ERROR, UNSUPPORTED_EXPRESSION, TRANSITION_NOT_FOUND` (theo yêu cầu)
cùng `INVALID_GUARD` (cú pháp/cấu trúc sai), `EVALUATION_ERROR` (chia cho 0...). `INVALID_VARIABLE_TYPE` (tên cũ) là **bí danh** của `TYPE_ERROR`.
`guard_result` là `None` (không phải `False`) khi chưa đánh giá được.

## 4. Thay đổi ảnh hưởng tương thích

Giữ nguyên: `GuardEvaluator(path)`, `load_model`, `get_transition`, `evaluate_guard`, `check_transition`, `check_invariants(variables, invariant_ids)`, `apply_transition(...)`, `GuardCheckResult.to_dict()`.

Thay đổi cần biết:
1. `InvariantStatus` đổi từ `SKIPPED_MISSING_VARIABLE`/`INVALID_VARIABLE_TYPE` sang bộ `SATISFIED/VIOLATED/SKIPPED/UNSUPPORTED/ERROR` + `reason_code` (theo yêu cầu mới; không thể giữ bí danh vì một trạng thái cũ tách thành hai).
2. `GuardEvaluator(..., strict=True)` mặc định **từ chối mô hình có guard/action/invariant lỗi**. `strict=False` giữ lỗi ở `model_issues`.
3. Biểu thức ngoài whitelist trước đây ra `INVALID_GUARD`, nay ra `UNSUPPORTED_EXPRESSION`.
4. `TransitionOutcome` thêm `backend_effects`, `error_status`, `missing_variables`, `invariant_report`, `simulated`; action lỗi làm **toàn bộ transition không áp dụng** (atomic).
5. API mới: `get_transition_variables`, `get_guard_variables`, `get_negative_transition`, `invariant_ids`, `negative_transition_ids`, `check_invariants(..., current_state=, scopes=)`.

## 5. Kết quả kiểm thử

| Nhóm | File / lớp test | Số test | Kết quả |
|---|---|---|---|
| A. Model loading | `TestModelLoading` | 20 | pass |
| B. Guard (biên, AND/OR/NOT, thiếu biến, sai kiểu, không hỗ trợ, chia 0, không-eval) | `TestGuardEvaluation` | 71 | pass |
| C. Transition | `TestTransitionEvaluation` | 14 | pass |
| D. Invariant | `TestInvariants` | 23 | pass |
| E. Kịch bản nghiệp vụ BlueMoon (10 kịch bản trong yêu cầu + luồng đầy đủ) | `TestBlueMoonBusinessScenarios` | 20 | pass |
| F. Mô phỏng `apply_transition` | `TestApplyTransition` | 14 | pass |
| G. Truy vết `security_rules.json` + cấu trúc mô hình | `TestTraceabilityAndStructure` | 12 | pass |
| Conformance với backend thật (integration) | `test_efsm_backend_conformance.py` | 33 | pass |
| API gốc | `tests/test_api.py` | 5 | pass |
| **Toàn bộ** (`python -m pytest -v` và `pytest`) | | **212** | **pass** |

Các lệnh đã chạy: `python -m py_compile guard_evaluator.py` (OK), `python -m pytest tests/test_guard_evaluator.py -v`, `python -m pytest tests/test_api.py -v`, `python -m pytest -v`.

**Kiểm tra độ nhạy của test:** mình cố ý làm hỏng evaluator 5 kiểu (đổi `<=` thành `<`, bỏ phủ định `not`, biến thiếu-dữ liệu thành VIOLATED, bỏ kiểm tra state nguồn, guard luôn SATISFIED). Test phát hiện 4/5 ngay; kiểu còn lại (`not` trong `expr`) chưa có test nên mình đã bổ sung `test_not_inside_expression`. Đây là kiểm tra thủ công, chưa phải mutation testing đầy đủ.

Bằng chứng thực nghiệm cho các sai lệch của v1: chạy thật cho thấy `pay` với kết quả không hợp lệ trả **400** (v1 ghi 422), `PATCH` dòng giỏ không tồn tại trả **404** (v1 không có điều kiện), `DELETE /api/cart` trả **200** (v1 không có transition).

## 6. Vấn đề chưa giải quyết / cần bạn quyết định

1. **Chính sách `payment_method` (REQUIREMENT_AMBIGUITY):** `pay_order` không đọc `order.payment_method`; đơn `COD` vẫn "thanh toán" được bằng mô phỏng. Chưa có rule nên mô hình mô tả đúng hành vi hiện tại.
2. **Thanh toán thất bại không giới hạn số lần thử** — chưa có rule giới hạn.
3. **NT04 không đạt được qua API** (`api_reachable: false`): cần seed DB hoặc mutant BUG-04. Tương tự **BUG-10** (giá/số tiền do client gửi): API không có trường này nên không có negative qua API; chỉ phát hiện được bằng mutant ở backend.
4. **Trừu tượng hóa một dòng hàng** (`ordered_quantity`, `stock`): bộ sinh test nên sinh đơn một dòng cho đường đi chính; đơn nhiều dòng chỉ được kiểm tra ở INV-03 qua `order_items`.
5. **Transition lỗi 404 do `order_id`/`product_id` không tồn tại** chỉ được mô hình hóa cho sản phẩm (`product_exists`); đơn không tồn tại chưa được mô hình hóa.
6. **Race condition / đồng thời** (hai request cùng trừ kho) không nằm trong EFSM tuần tự này.
7. `INV-06/07/08/10` kiểm tra **mô hình**, không kiểm tra backend; phần backend dựa vào negative tương ứng. `security_rules.json` chưa có tham chiếu ngược tới transition (mình không sửa file này; truy vết nằm ở phía mô hình và được test).
8. Các test conformance dùng `test_online_shop.db` tương đối theo thư mục chạy — hãy chạy pytest từ thư mục gốc dự án như `test_api.py`.

## 7. Ảnh hưởng tới `test_path_generator.py`

* Dùng `get_transition_variables(tid)["required_inputs"]` để biết cần sinh biến nào; `conditional_action_reads` chỉ cần khi nhánh `when` chạy (T01 với dòng mới).
* Luồng hợp lệ: `T01/T02/T03/T10 (CART) → T04 → T05 → T06|T07 → ... → T08/T09`. `T04` bắt buộc có `order_items` và các `*_length`; sau `T04` mô phỏng tự khởi tạo `order_status`, `cancel_count`, `payment_count`, `stock_restoration_count`.
* Dùng `check_negative_transition(NTxx, state, vars)` để biết bộ dữ liệu có thực sự kích hoạt ca âm tính hay không; `base_transition`, `expected.http_status`, `api_reachable` có sẵn trong mô hình.
* Kiểm tra `outcome.backend_effects`/`skipped_actions` — chúng là phần **tầng thực thi API phải tự xác minh**, evaluator không mô phỏng.
* Thứ tự lỗi của backend (422 → 404 → 409/400) quyết định mã trả về khi một request vi phạm nhiều điều kiện; negative nên chỉ vi phạm **một** điều kiện.

## 8. Chạy trên Windows bằng VS Code

```cmd
cd WEB_Project1
py -3.12 -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python -m py_compile guard_evaluator.py
python -m pytest tests/test_guard_evaluator.py -v
python -m pytest tests/test_api.py -v
python -m pytest tests/test_efsm_backend_conformance.py -v
python -m pytest -v
python guard_evaluator.py
```

Yêu cầu **Python 3.11+** (dùng `enum.StrEnum`; đã chạy trên 3.12). Trong VS Code: `Ctrl+Shift+P` → *Python: Select Interpreter* → chọn `.venv`; panel *Testing* → *Configure Python Tests* → pytest → thư mục gốc.
