# Thiết kế Test Data Generator — Phần 2/2 (sinh dữ liệu theo test path)

Tài liệu này mô tả phần **path-aware** của `test_data_generator.py`. Phần lõi mức guard (EP/BVA, solver `SAT/UNSAT/UNKNOWN/UNSUPPORTED`) được mô tả trong `docs/test_data_generator_core_design.md` và **được dùng lại nguyên vẹn**; Phần 2 chỉ thêm một tham số tùy chọn cho `solve()` (xem mục 8).

## 1. Vị trí trong pipeline

```
Business Workflow → EFSM → GuardEvaluator → TestPathGenerator → TestDataGenerator → API Test Executor → Result Validator
                                                    │                    │
                                              test_paths.json      test_cases.json (+ api_test_mapping.json)
```

Đầu vào: `efsm_model.json`, `security_rules.json`, `test_paths.json` (không sửa), `api_test_mapping.json`.
Đầu ra: `test_cases.json`, `test_data_coverage.json`. Không gửi HTTP request, không chạm database, không có executor hay validator.

```
python test_data_generator.py --model efsm_model.json --test-paths test_paths.json \
    --test-cases-output test_cases.json --coverage-output test_data_coverage.json [--max-cases N] [--max-setup-steps 3]
```

Không có `--test-paths` thì chương trình chạy đúng như Phần 1 (kết quả `generated_guard_data.json` giữ nguyên từng byte, có test kiểm tra).

## 2. Các thành phần

| Thành phần | Vai trò |
|---|---|
| `ApiMapping` | Nạp `api_test_mapping.json`; `validate(model)` đối chiếu tên path param / body / op / capture với EFSM. |
| `ShadowState` | Mô phỏng dữ liệu nghiệp vụ: giỏ, tồn kho, đơn, thanh toán. Cập nhật bằng các quy tắc có cấu trúc (`cart_add`, `cart_set`, `cart_remove`, `cart_clear`, `order_create`, `order_status`, `order_cancel`, `payment_record`) và chiếu (`project`) sang biến EFSM. |
| `PlanState` | Một nhánh kế hoạch: `ShadowState`, trạng thái EFSM, các bước đã dựng, bảng capture. Được sao chép khi rẽ nhánh nên quay lui không làm bẩn nhánh khác. |
| `PathDataGenerator` | Lập kế hoạch dữ liệu cho path positive / negative, dựng bản ghi test case. |
| `generate_test_cases()` / `build_coverage()` | Chạy mọi path, chọn lọc, dựng `test_cases.json` và `test_data_coverage.json`. |

## 3. Ba loại dữ liệu

Mỗi bước ghi rõ trong `data_classification`:

| Loại | Ý nghĩa | Ví dụ |
|---|---|---|
| `input` | Do test chọn và gửi lên. Biến `origin: request` của EFSM, cộng `product_id` lấy từ catalog seed. | `quantity`, `payment_method`, độ dài `customer_name` |
| `derived` | Biến EFSM suy ra từ `ShadowState` (không gửi lên). | `cart_items_count`, `stock`, `order_total`, `ordered_quantity` |
| `backend_generated` | Do backend tạo ra khi chạy. **Không bao giờ có giá trị cụ thể**, chỉ có tham chiếu. | `order.id`, `payment.id` |

`product_id` 1..3 là dữ liệu seed có sẵn trong `app/database.py` (`api_test_mapping.json › catalog`), không phải ID bịa. ID do backend sinh (đơn hàng, thanh toán) chỉ xuất hiện dưới dạng tham chiếu.

## 4. Cập nhật ngữ cảnh xuyên suốt các transition

Vấn đề: dữ liệu ở T01 (số lượng thêm vào giỏ) phải khớp với T04 (đơn hàng được tạo từ giỏ) và T08 (hoàn kho). Cơ chế không viết riêng cho từng cặp transition:

1. **Chiếu**: `ShadowState.project(pid, view)` cho giá trị mọi biến EFSM mà bước đó cần (`current_qty`, `stock`, `order_status`, …). `view = cart` hay `order` suy từ `lifecycle` của trạng thái EFSM.
2. **Giải đầu vào**: biến `request` được giải bằng solver Phần 1 trên guard của transition, với ngữ cảnh đã chiếu làm `fixed` và `input_hints` làm gợi ý. Ví dụ `current_qty + quantity <= stock` với `current_qty = 2`, `stock = 10`.
3. **Xác minh guard**: `GuardEvaluator.check_transition` phải trả `GUARD_SATISFIED`; không thì ứng viên bị loại.
4. **Mô phỏng hai lần, đối chiếu**: (a) `GuardEvaluator.apply_transition` chạy các `assign` của EFSM; (b) `ShadowState` chạy `ops` trong mapping. Mọi biến mà EFSM `assign` đều được so sánh giữa hai bên. Lệch, hoặc biến được gán mà `ShadowState` không chiếu được, thì bước bị loại với mã `UNRESOLVED_CONTEXT`. Nhờ vậy một lỗi trong mô phỏng dữ liệu (ví dụ hủy đơn quên hoàn kho) bị phát hiện thay vì âm thầm sinh dữ liệu sai (có 2 test mutant kiểm tra).
5. **Đơn nhiều dòng**: T04 áp guard cho **từng** dòng giỏ; `ordered_quantity`/`stock` của EFSM là trừu tượng hóa theo một dòng.
6. **Giá trị kỳ vọng**: HTTP status lấy từ EFSM (`http_status_source`); tổng tiền, trạng thái, tồn kho lấy từ mô phỏng (`source: shadow_state_simulation`). Chưa có giá trị nào được kiểm chứng trên backend.

Action kiểu `effect` (UPSERT, INSERT…) không được `GuardEvaluator` mô phỏng; chúng được liệt kê trong `unsimulated_backend_effects` và được `ShadowState` thay thế bằng `ops`. Nếu một transition mới có `assign` mà `ShadowState` không biết, kết quả là `UNRESOLVED_CONTEXT` chứ không phải dữ liệu đoán.

## 5. Tham chiếu ký hiệu (symbolic reference)

Dạng: `$ref:steps.<step_no>.<đường dẫn response>`, ví dụ `$ref:steps.2.response.order.id`.

* `capture` trong mapping khai báo bước nào tạo ra giá trị (T04 → `order_id`, T06/T07 → `payment_id`).
* Bước sau dùng giá trị ở `request.path_params` (và ghi thêm vào `symbolic_refs` với `captured_from_step`).
* Số bước tính trên **toàn bộ** `steps` của test case (kể cả bước setup), nên tham chiếu luôn trỏ đúng bước.
* Trong `ShadowState` đơn được định danh bằng chính chuỗi tham chiếu, nên `assertions` và `payments` cũng dùng ký hiệu thay vì ID giả.
* API Executor thay tham chiếu bằng giá trị thật lấy từ response của bước đó.

## 6. Lập kế hoạch (tìm dữ liệu)

`test_paths.json` được sinh với một ngữ cảnh mẫu, nên 119 path positive bị đánh dấu `INFEASIBLE` (ví dụ T04 trên giỏ trống). Generator **không bỏ** chúng mà lập kế hoạch lại:

* **Biến thể setup**: thử lần lượt `[]`, `[T01(p1)]`, `[T01(p1), T01(p2)]`, … tối đa `--max-setup-steps` (mặc định 3) bước. Setup luôn là lời gọi API (transition T01 là self-loop tại trạng thái đầu, khai báo trong `setup_transitions`) — không dùng fixture khi API làm được.
* **DFS quay lui** qua các bước của path; mỗi bước thử các `product_id` ứng viên (`catalog` cho T01, dòng trong giỏ cho T02/T03, dòng tham chiếu cho T04…).
* Ngân sách `--max-path-nodes` (mặc định 3000). Hết ngân sách → `SEARCH_BUDGET_EXHAUSTED`, **không** phải lời kết luận vô nghiệm.

Kết quả của path không lập được là `BLOCKED` với `reason_code`, `evidence` và `proof_scope`. Đây là tìm kiếm có giới hạn trên catalog seed và tối đa 3 bước setup, không phải chứng minh tổng quát.

## 7. Test case negative

Một test case negative = **tiền tố positive hợp lệ** + **đúng một bước vi phạm**.

1. Tiền tố được lập kế hoạch như path positive (mỗi bước được `GuardEvaluator` xác minh).
2. Với bước âm tính, thử từng `product_id` ứng viên (kể cả sản phẩm chưa có trong giỏ và `nonexistent_product_id`):
   * Giải đầu vào **hợp lệ** của transition cơ sở (`_valid_inputs`); chỉ giữ biến `request` mà request thực sự dùng (không thêm khóa thừa).
   * Nếu `check_negative_transition` đã `NEGATIVE_APPLICABLE` (vi phạm do trạng thái/ngữ cảnh) thì không đổi đầu vào — `technique = STATE_OR_CONTEXT_VIOLATION`.
   * Nếu chưa, giải `invalid_condition` bằng solver với `mutable` = các biến request xuất hiện trong điều kiện, giữ nguyên các biến còn lại; giá trị biên lấy từ solver (BVA, ví dụ `quantity = stock + 1`); biến enum dùng `allow_off_domain` → `technique = EP_BVA_INPUT_VIOLATION`.
   * Xác minh lại bằng `check_negative_transition` = `NEGATIVE_APPLICABLE`.
3. **Cô lập vi phạm** (`violation.isolation`): đánh giá guard của transition cơ sở trên dữ liệu đã chọn. `ISOLATED` nếu mọi điều kiện bị vi phạm đều liên quan đến biến của `invalid_condition`; `NOT_ISOLATED` nếu còn điều kiện khác sai; `UNEVALUATED` nếu guard không đánh giá được (thiếu biến). Planner duyệt các biến thể setup để ưu tiên `ISOLATED` (ví dụ NT16 `quantity > stock` cần dòng giỏ có sẵn để `cart_item_exists` vẫn đúng).
4. Kỳ vọng: `http_status` từ `negative_transitions[].expected.http_status`; `state_after == state_before`; `data_after == data_before`; `assertions` kiểm tra giỏ, tồn kho, thanh toán, đơn không đổi và không có tác dụng phụ.

**Fixture (NT04)**: `ordered_quantity > stock` không đạt được chỉ qua API. Generator thêm bước `FIXTURE` (`via_api: false`, `status: REQUIRES_FIXTURE`) đặt `products.stock` nhỏ hơn số lượng trong giỏ (giá trị lấy từ solver), mô tả bằng `target {table, column, where}`. Chỉ mô tả; không thực thi. Test case ở trạng thái `PARTIALLY_READY`.

## 8. Thay đổi duy nhất trong lõi Phần 1

`TestDataGenerator.solve(..., allow_off_domain=False)`: khi bật, ứng viên của biến enum/chuỗi có thêm `OFF_DOMAIN_VALUE`, và việc duyệt hết miền hữu hạn **không** còn bị coi là chứng minh `UNSAT`. Mặc định tắt nên hành vi cũ không đổi; cờ được đặt lại trong `finally`. Cần cho NT12 (`payment_result not in {SUCCESS, FAILED}`) và NT13. Test: `test_off_domain_makes_enum_violation_solvable`, `test_part1_guard_data_is_byte_identical`.

## 9. Trạng thái sẵn sàng (readiness)

| Giá trị | Điều kiện |
|---|---|
| `READY` | Mọi bước có dữ liệu, mọi guard/negative được `GuardEvaluator` xác minh; negative được `ISOLATED`; không cần fixture. |
| `PARTIALLY_READY` | Có dữ liệu nhưng cần fixture (`REQUIRES_FIXTURE`) hoặc cô lập vi phạm chưa đánh giá được (`NEGATIVE_ISOLATION_UNEVALUATED`). |
| `BLOCKED` | Không có dữ liệu thỏa (`NO_FEASIBLE_DATA_FOUND`, `SEARCH_BUDGET_EXHAUSTED`) hoặc mô phỏng không đối chiếu được (`UNRESOLVED_CONTEXT`). |
| `UNSUPPORTED` | Transition không có trong EFSM / không có cách gọi API trong mapping / guard hoặc fixture chưa hỗ trợ. |

`READY` chỉ có nghĩa là dữ liệu nhất quán với EFSM, **không** có nghĩa backend sẽ qua test. `BLOCKED`/`UNSUPPORTED` không nằm trong `test_cases[]` mà nằm trong `unconverted_paths[]` kèm lý do.

## 10. Invariant

Mỗi bước có `invariant_checkpoint`: `simulated_status` (kết quả `GuardEvaluator.check_invariants` trên dữ liệu mô phỏng) và `runtime_status = NOT_VERIFIED_UNTIL_EXECUTION`. Với `kind: model_structure` (INV-06, 07, 08, 10) trạng thái `SATISFIED` chỉ nói về cấu trúc mô hình; backend được kiểm chứng qua các negative test ghi trong `covered_by`. `invariant_assertions` mô tả cách kiểm tra sau khi chạy (GET probe); mọi mục ở trạng thái `NOT_EXECUTED`. Không có invariant nào được ghi là đã thỏa trên backend.

## 11. Chọn test case

* **Loại trùng**: chữ ký lời gọi (`request_signature`) giống nhau → bản sau bị loại (`DUPLICATE`).
* **Giới hạn `--max-cases N`**: chọn tham lam theo thứ tự ưu tiên (1) negative, (2) nhiều transition chưa được phủ nhất, (3) rule có severity cao chưa được phủ, (4) severity, (5) thứ tự path. Path bị bỏ ghi `NOT_SELECTED_MAX_CASES`.
* Mặc định không giới hạn: giữ mọi test case chuyển đổi được. ID `TC001…` đánh số theo thứ tự path nên ổn định giữa các lần chạy.

## 12. Schema `test_cases.json` (rút gọn)

```
generator, schema_version "1.0", scope, model{name,schema_version,sha256},
sources{test_paths{file,sha256,...}, api_mapping{file,sha256}}, configuration, conventions, summary, rules_index,
test_cases[] {
  test_case_id, source_path_id, path_key, type (positive|negative), readiness, readiness_reasons,
  negative_transition_id, negative_category, severity, security_rule_ids,
  source_feasibility, replanned,
  setup[]                       // mô tả các bước API/FIXTURE đứng trước path
  initial_data                  // dữ liệu seed
  steps[] {
    step_no, role (setup|path|negative|fixture), kind (transition|negative_transition|FIXTURE),
    transition_id | negative_transition_id, state_before, state_after,
    request {method, path, path_params, body}, request_data, symbolic_refs[], captures{},
    data_classification{input,derived,backend_generated},
    context_before, context_after,
    guard_check | violation{invalid_condition, technique, changed_variables, evaluator_status, isolation{...}},
    expected{outcome, http_status, http_status_source, state_after, response_checks[] | state_unchanged, data_unchanged},
    unsimulated_backend_effects[], invariant_checkpoint, data_after (+data_before cho negative), rule_ids
  },
  expected_outcome, expected_final_state, expected_final_data,
  assertions[], invariant_checkpoints[], invariant_assertions[], cleanup[], symbolic_refs[],
  execution {status: "NOT_EXECUTED"}
}
unconverted_paths[] {source_path_id, path_key, type, readiness, reason_code, reason, evidence, proof_scope}
```

## 13. API Executor sẽ dùng `test_cases.json` như thế nào

1. Nạp `api_test_mapping.json` (probe, cleanup) và `test_cases.json`; bỏ qua test case có `readiness` không mong muốn.
2. Với mỗi test case: dựng DB sạch (`cleanup[0]`, bằng fixture — backend không có API reset).
3. Duyệt `steps` theo `step_no`:
   * `kind = FIXTURE` → thực hiện thay đổi DB mô tả trong `target`/`value` (ngoài API).
   * Ngược lại → gửi `request.method` + `request.path` (thay `{param}` bằng `path_params`), body là `request.body`. Trước khi gửi, thay mọi chuỗi `$ref:steps.N.<path>` bằng giá trị lấy từ response đã lưu của bước N.
   * Lưu response; so với `expected.http_status` và từng `response_checks` (giá trị có thể chứa `$ref`).
4. Sau bước cuối: gửi các probe trong `assertions` (GET) và so với `expect`; với negative, so ảnh chụp trước/sau bước âm tính.
5. `invariant_assertions` mô tả cách kiểm invariant bằng probe; Result Validator quyết định `SATISFIED`/`VIOLATED`. Executor ghi kết quả vào `execution`.

## 14. Giới hạn đã biết

* `ShadowState` mô phỏng giỏ dùng chung, một đơn đang xử lý tại một thời điểm, đúng với các transition hiện có; chưa mô phỏng đồng thời nhiều đơn.
* `request_data` dùng độ dài chuỗi cho tên/điện thoại/địa chỉ (đúng theo biến EFSM) nên nội dung chuỗi là ký tự lặp (`NNN…`, `999…`, `AAA…`); chưa kiểm tra định dạng nội dung.
* Cô lập vi phạm `UNEVALUATED` cho NT03 và NT19 vì guard của T04/T01 thiếu biến khi giỏ trống / sản phẩm không tồn tại (không tự bịa giá trị).
* NT04 cần fixture DB; bước fixture chỉ được mô tả.
* 49 path positive `BLOCKED`: tất cả có dạng `... T10 ... T04` (T10 làm trống giỏ rồi T04 cần giỏ không trống mà path không có bước thêm hàng).
* Giá trị kỳ vọng ngoài HTTP status đến từ mô phỏng, chưa đối chiếu với backend thật (đó là việc của Executor/Validator).
* Mapping hiện chỉ có fixture cho biến `stock`; biến database khác cần bổ sung `fixtures` và hỗ trợ trong `ShadowState`.
