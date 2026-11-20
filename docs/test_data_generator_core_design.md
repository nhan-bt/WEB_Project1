# Thiết kế `test_data_generator.py` (phần lõi — mức guard)

Module sinh dữ liệu **cho một guard / một transition tại một thời điểm**: dữ liệu hợp lệ, dữ liệu vi phạm có chủ đích,
dữ liệu thiếu/sai kiểu, bằng Equivalence Partitioning (EP) và Boundary Value Analysis (BVA); mọi bộ dữ liệu đều được
**kiểm tra lại bằng `GuardEvaluator`**. Chưa sinh dữ liệu xuyên suốt test path (phần 2), không gửi HTTP, không truy cập database,
không dùng `eval/exec`. `test_path_generator.py`, `efsm_model.json`, `guard_evaluator.py`, `security_rules.json`, backend và database
**không bị sửa**.

## 1. Những gì đã đọc từ source (không giả định)

### Guard trong `efsm_model.json`
9 transition có guard, 1 transition (`T10`) không có guard (`null`). Mọi guard dùng DSL của evaluator:

| Transition | Guard (rút gọn) | Biến |
|---|---|---|
| T01 | `product_exists == true` ∧ `quantity > 0` ∧ **`expr: current_qty + quantity <= stock`** | `product_exists`, `quantity`, `current_qty`, `stock` |
| T02 | `product_exists` ∧ `cart_item_exists` ∧ `quantity > 0` ∧ **`quantity <= stock`** (`value_var`) | + `cart_item_exists` |
| T03 | `cart_item_exists == true` | |
| T04 | 10 điều kiện: `cart_items_count > 0`, `ordered_quantity > 0`, **`ordered_quantity <= stock`**, `payment_method in [MOCK, COD]`, 3 cặp biên độ dài | 9 biến |
| T05 | `order_status == PENDING_CONFIRMATION` | |
| T06, T07 | `order_status == CONFIRMED` ∧ `payment_result == SUCCESS` / `FAILED` | |
| T08, T09 | `order_status in [PENDING_CONFIRMATION, CONFIRMED]` | |

Guard phụ thuộc nhiều biến: T01 (biểu thức 3 biến), T02 (`quantity` ↔ `stock`), T04 (`ordered_quantity` ↔ `stock`).

### Interface của `GuardEvaluator` (dùng đúng như hiện có)
* Runtime context là `Mapping[str, Any]`; evaluator **không** gán giá trị mặc định.
* `evaluate_guard(guard, variables) -> GuardEvaluation(status, result, failed_conditions, ...)` và
  `check_transition(tid, state, variables) -> GuardCheckResult(status, guard_result, ...)`.
* Thứ tự kiểm tra: cấu trúc guard → biến thiếu → sai kiểu → đánh giá. Biến thiếu → `MISSING_VARIABLE`; `None`/sai kiểu (kể cả `bool` cho số nguyên) → `TYPE_ERROR`;
  construct ngoài whitelist → `UNSUPPORTED_EXPRESSION`; guard sai cấu trúc → `INVALID_GUARD`.
* Toán tử DSL: `== != > >= < <= in not_in exists not_empty`; logic `all/any/not`; lá `expr` (ast whitelist: so sánh, `and/or/not`, `+ - * /`, hằng số, tên biến/enum, danh sách, `SUM_PRODUCT`).
* `get_guard_variables`, `get_variable_spec` cho biến/kiểu/`origin`/`allowed_values`.

### Miền giá trị
EFSM khai báo **kiểu** và `allowed_values` (chuỗi) nhưng **không khai báo min/max cho bất kỳ biến số nào**. Vì vậy: miền số được ghi là `UNDETERMINED_IN_EFSM`; boolean có miền hữu hạn; chuỗi có `allowed_values` có miền hữu hạn khai báo; chuỗi không khai báo là `UNDETERMINED_IN_EFSM`. Module không tự đặt ràng buộc nghiệp vụ nào.

### Invariant phụ thuộc dữ liệu runtime
`analyze_invariants()` liệt kê biến cần có của từng invariant. `INV-03` cần `order_items` (danh sách) và `SUM_PRODUCT` → **bộ sinh chưa hỗ trợ** (kiểu `list`, hàm). `INV-06/07/08/10` kiểm tra cấu trúc mô hình (không áp dụng). Các invariant còn lại (`INV-01/02/04/05/09/11/12`) dùng biến `derived`/`database` như `payment_count`, `stock_after`, `payment_amount`.

## 2. Biểu diễn trung gian (IR) và phân tích

Guard JSON được chuyển thành IR nhỏ: `Atom(op, left, right)`, `And`, `Or`, `Not`, với term là biến / hằng / danh sách / `+ - * /`. IR **chỉ dùng để phân tích và dự đoán kết quả**; nó độc lập với evaluator nên có thể đối chiếu. Construct ngoài IR (`exists`, `not_empty`, `SUM_PRODUCT`, biến kiểu `list`…) → guard `UNSUPPORTED` kèm lý do (GuardEvaluator vẫn có thể hỗ trợ chúng; giới hạn nằm ở bộ sinh).

`analyze_guard(guard)` trả về, cho mỗi biến: `type`, `origin`, **`role`**, `domain`, và danh sách `constraints` (điều kiện, toán tử, `constant_bound` nếu là biên hằng, `depends_on_variables`, `linear`); cùng danh sách `conjuncts` (các điều kiện cấp cao).

* **`role = input`**: biến có `origin = request` (hoặc chưa rõ nguồn) — biến cần sinh.
* **`role = context`**: `database`/`derived` (ví dụ `stock`) — **không bao giờ được coi là hằng số**; giá trị lấy từ `context` do người dùng cung cấp hoặc tìm kiếm, và được ghi lại.

Ví dụ `quantity > 0 AND quantity <= stock`: `quantity` là input có biên hằng `> 0` và quan hệ `<= stock`; `stock` là context phụ thuộc.

## 3. Bộ giải ràng buộc hữu hạn (`solve`)

1. Chuyển về **DNF** (đẩy `NOT` xuống nguyên tử; giới hạn `max_dnf_terms`, vượt → `UNKNOWN`).
2. Với mỗi hạng tử (một hội các nguyên tử), thay biến cố định bằng giá trị rồi **tuyến tính hóa** nguyên tử (`+ - *` với hằng) để rút biên một biến.
3. **Chứng minh UNSAT** (chỉ ở hai trường hợp):
   * *Suy luận khoảng số nguyên*: giao các biên của một biến (kể cả `!=`, `in`, `not_in`); rỗng ⇒ vô nghiệm. Miền số nguyên là hoàn chỉnh (mọi số nguyên), nên đây là chứng minh.
   * *Duyệt hết miền hữu hạn đã khai báo* (boolean, chuỗi có `allowed_values`).
4. **Tìm nghiệm**: tách thành các thành phần liên thông theo biến chung; mỗi thành phần tìm bằng DFS có cắt tỉa trên tập ứng viên hữu hạn, thứ tự ưu tiên: gợi ý ngữ cảnh → trung điểm/biên của khoảng suy ra → ngưỡng của các nguyên tử → hằng và **ước số của hằng** (cho ràng buộc nhân) → giá trị của biến cố định ±1 → `int_pool` (`0, 1, -1, 2, 5, 10, 100`; chỉ là ứng viên tìm kiếm, không phải miền nghiệp vụ).

Kết quả: `SAT` (kèm nghiệm), `UNSAT` (kèm lý do và `assumptions` về biến cố định), `UNKNOWN`, `UNSUPPORTED`.
**Hết ngân sách (`max_nodes`) hoặc hết ứng viên với miền chưa đầy đủ luôn là `UNKNOWN`, không bao giờ là `UNSAT`** (có test, và có test đột biến chứng minh).

## 4. Sinh dữ liệu (`generate_for_guard` / `generate_for_transition`)

1. **Bộ dữ liệu nền (BASE)**: một nghiệm hợp lệ. Biến context do người dùng cung cấp được giữ **cố định** trước; nếu làm guard vô nghiệm, thả lỏng thành gợi ý và ghi `base.context_adjusted`.
2. **Biến được thể hóa** (mỗi lần đổi đúng một biến, các biến khác giữ nguyên bộ nền): với mỗi điều kiện cấp cao, biến `input` của điều kiện đó; nếu điều kiện không có input (guard trạng thái như `order_status == ...`) thì mọi biến của điều kiện.
3. **Số nguyên — BVA + EP tính từ guard** (không hardcode giá trị):
   * Với mỗi nguyên tử chứa biến `v`, rút biên `v op k` (các biến khác lấy từ bộ nền) rồi tính **điểm cắt** nơi giá trị thật của nguyên tử đổi: `>= k → ⌈k⌉`, `> k → ⌊k⌋+1`, `<= k → ⌊k⌋+1`, `< k → ⌈k⌉`, `== / !=`, `in / not_in`.
   * Các điểm cắt chia trục số thành các **đoạn tương đương** `[lo, hi]`. BVA lấy `lo, lo+1, hi-1, hi` của từng đoạn; EP lấy một đại diện bên trong (trung điểm; với đoạn vô hạn một phía lấy `lo+1` hoặc `hi-1`).
   * Nhãn `expected_guard` của mỗi giá trị được **dự đoán độc lập bằng IR**.
   * Ví dụ T01 với `stock = 10`, `current_qty = 0`: biên suy ra từ `quantity > 0` (cắt tại 1) và `current_qty + quantity <= stock` (cắt tại 11) cho các đoạn `[-inf,0]`, `[1,10]`, `[11,+inf]` và các giá trị `-1, 0, 1, 2, 9, 10, 11, 12` (cùng đại diện `5`). Đổi `stock = 5` thì biên tự dời sang `4, 5, 6`; đổi `current_qty = 4` thì biên trên là `6/7`.
4. **Boolean** và **chuỗi/enum (EP)**: mỗi giá trị trong miền khai báo (và hằng trong guard) là một mẫu; thêm một giá trị **ngoài miền khai báo** (`__OUT_OF_DOMAIN__`) cho phân vùng không hợp lệ. Giá trị trong miền nhưng không thỏa guard (ví dụ `FAILED` với `payment_result == SUCCESS`) là phân vùng không hợp lệ khác.
5. **Thiếu / null / sai kiểu**: chỉ cho biến `input`. Kỳ vọng là `expected_status` (`MISSING_VARIABLE`, `TYPE_ERROR`), không phải `expected_guard` (guard không đánh giá được, không phải "sai").
6. **Mỗi điều kiện cấp cao có ít nhất một mẫu chỉ vi phạm điều kiện đó**: nếu một-biến-mỗi-lần chưa tạo ra, bộ giải tìm `Not(điều kiện) ∧ các điều kiện còn lại`; nếu không được thì ghi vào `conjunct_notes` (`UNSAT`/`UNKNOWN`) chứ không im lặng.

Mẫu trùng dữ liệu được gộp (`techniques` liệt kê mọi kỹ thuật, `technique` là kỹ thuật đầu). Thứ tự mẫu ổn định; `sample_id` là `GD001…` liên tục.

## 5. Xác minh bằng GuardEvaluator

Generator **không tự quyết định VERIFIED**. Mỗi mẫu được đánh giá bằng `check_transition` (khi có transition) hoặc `evaluate_guard`; chỉ khi kết quả khớp kỳ vọng mới được gán nhãn:

| Kỳ vọng | Evaluator trả về | `verification_status` |
|---|---|---|
| `expected_guard = true` | `GUARD_SATISFIED` | `VERIFIED_VALID` |
| `expected_guard = false` | `GUARD_NOT_SATISFIED` | `VERIFIED_INVALID` |
| `expected_status = MISSING_VARIABLE / TYPE_ERROR` | đúng status đó | `VERIFIED_INVALID` |
| bất kỳ | `UNSUPPORTED_EXPRESSION` / `INVALID_GUARD` | `UNSUPPORTED` |
| còn lại (kể cả không dự đoán được) | | `UNVERIFIED` |

Với mẫu không hợp lệ, từng điều kiện cấp cao còn được đánh giá riêng bằng evaluator: `violated_conditions.predicted` (IR) so với `.verified` (evaluator); `single_condition` chỉ là `true` khi evaluator xác nhận đúng một điều kiện bị vi phạm. Nếu dự đoán ≠ evaluator, mẫu bị hạ xuống `UNVERIFIED`. Không mẫu nào được "mặc định" là vi phạm đơn.

## 6. Cấu trúc `generated_guard_data.json`

Không có timestamp (kết quả giống hệt từng byte giữa các lần chạy).

```
generator, schema_version "1.0", scope, model{name, schema_version, sha256}, configuration{...},
summary{guards_analyzed/supported/unsupported, samples_total, valid/invalid/malformed_samples,
        verification{...}, verified_by_guard_evaluator, single_condition_violations_verified, unsolved[]},
rules_index{}, invariant_analysis[],
datasets[ { schema_version, transition_id, label, source_state, event, rule_ids, guard, status, reason,
            analysis{...}, base{...}, conjunct_notes[], samples[ {
              sample_id, data, expected_guard, expected_status, technique, techniques, partition,
              target_variable, changed_variables, violated_conditions{predicted, verified, single_condition,
              prediction_matches_evaluator}, verification_status, evaluator_verdict{status, guard_result}, notes } ] } ]
```

`status` của dataset: `OK`, `NO_GUARD`, `UNSUPPORTED`, `UNSAT`, `UNKNOWN`.

## 7. Interface cho phần 2

```python
gen = TestDataGenerator("efsm_model.json", "security_rules.json", max_nodes=20000)
analysis = gen.analyze_guard(guard)                               # GuardAnalysis (.to_dict())
result   = gen.solve(guard, fixed={"stock": 10}, want_violation=False)   # SolveResult: SAT/UNSAT/UNKNOWN/UNSUPPORTED
dataset  = gen.generate_for_transition("T01", {"stock": 10, "current_qty": 0})   # GuardDataSet
dataset  = gen.generate_for_guard({"expr": "..."}, context, label="NT05")        # guard bất kỳ, kể cả invalid_condition
datasets = gen.generate_all(defaults, per_transition_context)     # toàn bộ transition, sample_id liên tục
out      = gen.to_output_dict(datasets); TestDataGenerator.export_json(out, "generated_guard_data.json")
```

Phần 2 có thể dùng: `dataset.base.assignment` (điểm xuất phát hợp lệ), `valid_samples()`, `invalid_samples()`, `samples[i].data / changed_variables / violated_conditions`, `analysis.variables[*].role/origin` để biết biến nào là input (đặt vào request) và biến nào phải chuẩn bị trong DB/bước trước, và `solve(..., fixed=...)` để chọn dữ liệu theo trạng thái thực sau từng bước của một path.

## 8. CLI

```powershell
python test_data_generator.py --model efsm_model.json --context test_data_context.bluemoon.json --output generated_guard_data.json
```

Tùy chọn: `--transition T01` (lặp lại được), `--no-negative`, `--no-malformed`, `--max-nodes`, `--rules`. File context: `{"defaults": {biến: giá trị}, "transitions": {"T01": {...}}}`. `test_data_context.bluemoon.json` là **ngữ cảnh ví dụ** (ưu tiên giá trị như `stock = 10`), không phải miền giá trị nghiệp vụ. Mã thoát `0` thành công, `2` lỗi đầu vào.

## 9. Giới hạn

* Miền số không có min/max trong EFSM ⇒ UNSAT chỉ chứng minh được bằng suy luận khoảng một biến; ràng buộc nhiều biến, phi tuyến, hay chia chỉ có thể `SAT` hoặc `UNKNOWN`. Ràng buộc nhân dùng ước số của hằng; nghiệm cần giá trị ngoài tập ứng viên (ví dụ `x * z == 3000009`) cho `UNKNOWN` dù tồn tại nghiệm.
* Biên BVA chỉ suy được từ nguyên tử tuyến tính khi các biến còn lại đã cố định; nguyên tử phi tuyến chỉ có ứng viên từ tìm kiếm (ghi chú `non-linear` trong `analysis.notes`).
* Biến `decimal` được xử lý trên lưới số nguyên (chưa sinh giá trị thập phân/epsilon); biến `list` và toán tử `exists`/`not_empty` chưa được hỗ trợ.
* Một-biến-mỗi-lần: không sinh tổ hợp nhiều biến cùng sai (ngoài tìm kiếm bổ sung cho điều kiện chưa có mẫu đơn).
* Dữ liệu ở mức guard của **một** transition; chưa bảo đảm nhất quán giữa các bước (ví dụ `quantity` ở T01 với `ordered_quantity`/`order_items` ở T04) — việc của phần 2.
