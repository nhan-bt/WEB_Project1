# Thiết kế `test_path_generator.py`

Module sinh **đường đi kiểm thử** (positive và negative) từ `efsm_model.json`. Nó không truy cập database, không gửi HTTP,
không dùng `eval/exec`, và không thay đổi website. Việc đánh giá guard dùng đúng interface hiện có của `GuardEvaluator`
(`check_transition`, `apply_transition`, `check_invariants`, `check_negative_transition`, `get_transition_variables`,
`get_guard_variables`, `get_variable_spec`). `efsm_model.json`, `guard_evaluator.py` và `security_rules.json` **không bị sửa**.

## 1. Cấu trúc thực tế của mô hình mà module sử dụng

Không giả định tên trường; module đọc đúng các trường sau của `efsm_model.json` (schema 2.0):

| Thành phần | Trường được dùng | Ghi chú |
|---|---|---|
| Initial state | `initial_state` (nếu thiếu: state duy nhất có `type == "initial"`) | BlueMoon: `CART` |
| States | `states[].id` | 5 state; `entity_type`/`lifecycle` không ảnh hưởng thuật toán |
| Positive transition | `transitions[].id/source/target/event/guard/actions/rule_refs/expected_http_status/api` | 10 cạnh (T01–T10) |
| Biến liên quan | do `GuardEvaluator.get_transition_variables` tính (khớp trường `variables` trong mô hình) | `required_inputs`, `guard_reads`, `action_writes` |
| Negative transition | `negative_transitions[].id/source/event/base_transition/invalid_condition/expected/rule_refs/mutant_refs/api_reachable/note` | 20 mục (NT01–NT20) |
| Origin của biến | `variables[].origin` = `request` / `database` / `derived` | dùng để phân loại negative và để biết biến nào không mang sang bước sau |
| Rule | `security_rules.json`: `rules[].rule_id/name/category/severity/status`, `excluded_rules[]` | ưu tiên (`severity`) chỉ lấy từ file này |

Điểm cần lưu ý khi phân tích (đã ghi nhận, **không sửa mô hình**):

1. **Mô hình không có trường mức độ ưu tiên** cho transition/negative; `severity` chỉ có ở `security_rules.json`, nên path chỉ có `severity` khi liên kết được tới rule.
2. **Không có liên kết dữ liệu giữa `quantity` (thêm giỏ) và `ordered_quantity`/`order_items` (tạo đơn)**: mô hình coi `ordered_quantity` là biến dẫn xuất độc lập. Vì vậy một đường đi như `T01>T04` chỉ nhất quán về dữ liệu nếu người cấp context chọn giá trị khớp nhau (xem `test_path_context.bluemoon.json`). Đây là giới hạn của mô hình (REPRESENTATION_GAP), không phải của generator.
3. **`T07` chỉ có action `effect`** (ghi payment FAILED, giữ nguyên status), không có `assign`; mô phỏng giữ nguyên biến.
4. **`NT04` có `api_reachable: false`**: kích hoạt được ở mức mô hình nhưng không đạt tới được chỉ qua API; được ghi vào path và coverage (`not_api_reachable`).
5. Negative `NT13`, `NT14`... có `note` về điều kiện tiên quyết; được chép sang path (`model_note`).
6. **Phiên bản mô hình:** module được xây và kiểm thử trên `efsm_model.json`/`guard_evaluator.py` **v2** (đã hợp nhất vào dự án). `workflow.json` rỗng (0 byte) không được dùng và đã bỏ khỏi cây thư mục.

## 2. Đồ thị (`EfsmGraph`)

* Đỉnh = state; cạnh = **chỉ positive transition**, mỗi cạnh lưu `transition_id, source, target, event, order` (thứ tự khai báo).
* Là *multigraph*: giữ nguyên **self-loop** (T01, T02, T03, T10 ở `CART`; T07 ở `CONFIRMED`) và **cạnh song song** (4 cạnh `CART→CART`).
* Truy vấn: `reachable_states`, `unreachable_states`, `dead_ends`, `self_loops`, `parallel_edges`, `has_cycle` (duyệt lặp, không đệ quy), `shortest_path`, `shortest_path_to_edge`.
* **Negative transition không bao giờ là cạnh** (có test): chúng chỉ được ghép vào *cuối* đường đi sau khi prefix đã tính xong.
* Mọi duyệt đều theo thứ tự khai báo ⇒ kết quả tái lập.

BlueMoon: 5 state đều đạt tới được; `PAID`, `CANCELLED` không có cạnh đi ra; có chu trình (do self-loop).

## 3. Định danh đường đi

`path_key` là chuỗi transition ID: `"T01>T04>T05>T06"`. Negative path: prefix + `!NTxx`, ví dụ `"T01>T04>T05>T06>!NT08"`; prefix rỗng cho `"!NT01"`.
Chuỗi state chỉ là thông tin phụ (`states`), vì nhiều transition có thể cùng source/target. `path_id` là `P###` (positive) hoặc `N###` (negative), đánh số theo thứ tự sinh.

## 4. Chiến lược

### 4.1 BFS (`generate_bfs_paths`)
Với mỗi state đạt tới được (trừ initial) và mỗi transition, tìm đường đi **ngắn nhất theo số transition**; gộp trùng; mặc định **loại các path là tiền tố thực sự của path khác** (`prune_prefixes`, độ bao phủ không đổi). Hai chế độ tìm prefix (`prefix_mode`):

* `structural`: BFS trên đồ thị. Nhanh, không cần dữ liệu, nhưng có thể **không thực thi được**: ví dụ đường ngắn nhất tới `PAID` là `T04>T05>T06`, trong khi `T04` yêu cầu giỏ không rỗng (cần `T01` trước).
* `feasible`: BFS trên **cấu hình (state, biến)** bằng mô phỏng `GuardEvaluator`; chỉ mở rộng những bước đã được chứng minh `OK` với context. Một lần khám phá cho ra đường đi khả thi ngắn nhất tới *mọi* state và transition. Với BlueMoon nó tự tìm ra `T01>T04>T05>T06`.
* `auto` (mặc định): `feasible` nếu có context, ngược lại `structural`. Đích không có đường khả thi trong giới hạn thì quay về đường cấu trúc (nhãn feasibility sẽ là INFEASIBLE/UNKNOWN).

Giới hạn: `max_depth` (độ dài tối đa; đích cần dài hơn bị bỏ và đếm lại), `max_paths`, `max_nodes` (số cấu hình khi tìm khả thi). Mọi giới hạn bị chạm đều được ghi vào `summary.limits_hit`.

### 4.2 DFS (`generate_dfs_paths`)
Liệt kê các **đường đi tối đại** từ initial state (không thể mở rộng thêm trong giới hạn). Duyệt **lặp** bằng stack tường minh (không lỗi đệ quy), theo thứ tự mô hình. Giới hạn: `max_depth`, `max_paths`, `max_visits_per_state` (tính cả đỉnh xuất phát), `max_transition_repeats`. Mỗi path ghi `dfs_termination`: `TERMINAL_STATE` (state không có cạnh ra) hoặc `LIMIT`. `truncated` chỉ bật khi thực sự còn đường đi bị cắt.
Ràng buộc: DFS với `max_paths` nhỏ lấy *N path đầu tiên theo thứ tự DFS*, nên thiên về các self-loop đầu mô hình; dùng BFS/negative để có độ phủ có chủ đích.

### 4.3 Negative paths (`generate_negative_paths`)
Với mỗi `negative_transitions[i]`: lấy **prefix dương ngắn nhất** tới `source` (theo `prefix_mode`), rồi thêm **đúng một** bước negative ở cuối; không sinh thêm bước sau. Bỏ qua (và ghi `warnings`) khi `source` không đạt tới được hoặc prefix dài hơn `max_depth`.

**Phân loại** (có thể nhiều nhãn; quy tắc dựa trên mô hình, không hardcode BlueMoon):

| Nhãn | Điều kiện |
|---|---|
| `INVALID_STATE_TRANSITION` | không có positive transition cùng `event` xuất phát từ `source` của negative |
| `GUARD_VIOLATION` | có positive transition cùng `event` và `source` (guard của nó bị vi phạm) |
| `BUSINESS_CONSTRAINT_VIOLATION` | `GUARD_VIOLATION` và `invalid_condition` dùng biến có `origin` là `database`/`derived` |
| `INPUT_VALIDATION_VIOLATION` | `invalid_condition` chỉ dùng biến `origin == request`, hoặc HTTP status của negative thuộc `input_validation_statuses` (mặc định `{422}`) |

`negative_category` là nhãn chính (ưu tiên: INVALID_STATE → BUSINESS → INPUT → GUARD); `negative_categories` là toàn bộ nhãn; `category_evidence` ghi bằng chứng. Với context, `expected_rejection.violated_transition_guards` chỉ ra **điều kiện con nào của guard bị vi phạm** (đánh giá từng nhánh của `all`; một nhánh sai là đủ kết luận guard sai kể cả khi nhánh khác chưa đủ dữ liệu).

**Hành vi kỳ vọng** (`expected_rejection`): `http_status` **chỉ lấy từ mô hình** (`expected.http_status`); nếu mô hình không có thì là `null` kèm `http_status_source: null` — không tự đặt. `forbidden_backend_effects` là các `effect` của *base transition* (những thay đổi không được xảy ra, ví dụ trừ kho, tạo payment). `expected_state_unchanged`/`expected_data_unchanged` chép từ mô hình.

## 5. Tính khả thi (`evaluate_path_feasibility`)

`structure` luôn là `STRUCTURALLY_VALID` (chuỗi không nối tiếp hợp lệ bị từ chối bằng `PathStructureError`). `feasibility` là **một giá trị khác**:

| Giá trị | Ý nghĩa |
|---|---|
| `FEASIBLE` | Có context; **mọi** bước: guard thỏa mãn với biến đã cập nhật từ các bước trước, action gán mô phỏng được, không invariant nào VIOLATED/ERROR; negative path: negative transition thực sự kích hoạt được. Kèm `witness` (đầu vào đã dùng) |
| `INFEASIBLE` | Một guard sai, một invariant bị vi phạm, hoặc negative không kích hoạt được — **với context đã cung cấp** (`proof_scope = GIVEN_CONTEXT`). Không phải chứng minh cho mọi dữ liệu |
| `UNKNOWN` | Không có context, thiếu biến, sai kiểu, action không mô phỏng được, lỗi biểu thức/invariant, hoặc (policy `strict`) có effect chưa mô phỏng |

Quy tắc bảo đảm tính thận trọng:

* **Không** FEASIBLE chỉ vì từng guard thỏa mãn riêng lẻ: trạng thái được cập nhật qua `apply_transition` rồi mới đánh giá bước kế tiếp (có test `A,B2`).
* Thiếu biến/sai kiểu **không** thành INFEASIBLE; `INFEASIBLE` chỉ khi một guard/invariant *đã được đánh giá* và sai.
* **Biến `origin == request` không mang sang bước sau** (tránh dùng nhầm đầu vào cũ); đầu vào mỗi bước lấy từ `default_inputs`, `step_inputs[<transition_id>]`, `step_inputs["<số bước>"]` (khóa theo số bước ưu tiên hơn).
* Invariant được kiểm tra sau mỗi bước (`verify_invariants`); VIOLATED ⇒ INFEASIBLE (`INVARIANT_VIOLATED`), ERROR ⇒ UNKNOWN.
* Module chỉ mô phỏng **biến EFSM**. Action `effect` (ghi DB…) **không** được thực thi; chúng được liệt kê trong `transition_catalog.*.unsimulated_effects` và `feasibility_detail.steps[].unsimulated_effects`. Không giả định database đã đổi.

`effects_policy`:
* `model_contract` (mặc định): coi `effect` là phần lưu trữ của các `assign` cùng transition (hợp đồng của mô hình v2, đã được `tests/test_efsm_backend_conformance.py` kiểm chứng với backend thật). `FEASIBLE` nghĩa là *khả thi trên mô phỏng biến EFSM*, **không** có nghĩa đã thực thi.
* `strict`: nếu đường đi có bất kỳ effect nào thì tối đa `UNKNOWN`; INFEASIBLE sau một effect cũng thành `UNKNOWN` (`UNCONCLUSIVE_AFTER_UNSIMULATED_EFFECTS`).

Không có SMT solver. Context là **một** bộ giá trị cụ thể; việc sinh nhiều bộ giá trị thuộc `test_data_generator.py`.

## 6. Liên kết Business Security Rules và coverage

* `security_rule_ids` của path dương = rule của mọi transition trên path (theo thứ tự xuất hiện); của path âm = `rule_refs` của negative (rule được *nhắm tới*). `severity` = mức cao nhất trong các rule đó (nếu có). Không tạo rule mới; ID rule lạ trong mô hình chỉ sinh `warnings`.
* `test_path_coverage.json` tách bạch **cấu trúc** (`covered_structural`, mọi path) và **khả thi** (`covered_feasible`, chỉ tính path có kết luận FEASIBLE — cách đếm thận trọng); có state, positive transition, negative transition, cặp transition liên tiếp.
* Rule: `linked_to_paths` / `not_linked` / `with_feasibility_witness` (rule thuộc ít nhất một path FEASIBLE; *không phải* dữ liệu test đã sinh) / `with_generated_test_data`, `executed_on_backend`, `verified_by_oracle` luôn **`NOT_MEASURED`**; `level = PATH_COVERAGE_ONLY`. Rule `excluded_rules` (NOT_APPLICABLE) được liệt kê riêng, không tính là đã bao phủ.
* `executed_coverage.status = NOT_MEASURED` (module không gọi API).

## 7. Cấu trúc `test_paths.json`

Không có timestamp (kết quả giống hệt từng byte giữa các lần chạy).

```
generator, schema_version, model{name, schema_version, sha256, initial_state},
configuration{strategy, prefix_mode, effects_policy, giới hạn, context_fingerprint, ...},
feasibility_assumptions[], summary{total_paths, paths_by_type/strategy/feasibility, limits_hit}, warnings[],
transition_catalog{T01:{event,source,target,guard,guard_reads,required_inputs,conditional_action_reads,
                        action_writes,unsimulated_effects,expected_http_status,api,rule_ids}, ...},
negative_catalog{NT01:{event,source,base_transition_id,invalid_condition,condition_variables,expected,rule_ids}, ...},
rules_index{rule_id:{name,category,severity,status}},
paths[]
```

Mỗi `paths[i]`:

| Trường | Ý nghĩa |
|---|---|
| `path_id`, `path_type` (`positive`/`negative`), `strategy`, `strategies[]` (khi BFS và DFS trùng), `path_key`, `goals[]` | định danh và mục tiêu BFS |
| `transition_ids`, `states` | positive: toàn bộ đường đi; negative: **prefix** (state cuối = source của negative) |
| `structure`, `feasibility`, `feasibility_detail` | xem mục 5; `detail` có `reason_code`, `proof_scope`, `failed_step`, `steps[]`, `final_variables`, `witness`, `negative_check` |
| `security_rule_ids`, `severity` | liên kết rule |
| `required_context` | `initial_variables` (biến phải có từ đầu), `step_inputs` (đầu vào request theo số bước), `negative_inputs` |
| `expected_outcome` | `ACCEPT_ALL_STEPS` hoặc `REJECT_FINAL_STEP` |
| `steps[]` | `step_index, kind, transition_id, event, from_state, to_state, expected_outcome, expected_http_status, required_inputs, rule_ids`; guard/effects/api tra trong `transition_catalog` bằng `transition_id` |
| `execution` | luôn `{"status": "NOT_EXECUTED"}` |
| (negative) | `negative_transition_id`, `negative_category`, `negative_categories`, `category_evidence`, `expected_rejection{...}`, `expected_state_unchanged`, `expected_data_unchanged`, `base_transition_id`, `mutant_refs`, `api_reachable`, `prefix_mode`, `model_note` |
| (DFS) | `dfs_termination` |

## 8. Giao diện Python và CLI

```python
gen = TestPathGenerator("efsm_model.json", "security_rules.json", effects_policy="model_contract")
ctx = PathContext.from_file("test_path_context.bluemoon.json")      # tùy chọn
gen.build_graph(); gen.generate_bfs_paths(ctx); gen.generate_dfs_paths(ctx, max_depth=8, max_paths=200)
gen.generate_negative_paths(ctx); gen.evaluate_path_feasibility(["T01", "T04"], ctx)
result = gen.generate(strategy="both", include_negative=True, context=ctx)
gen.calculate_coverage(result.paths); TestPathGenerator.export_json(gen.to_output_dict(result), "test_paths.json")
```

```powershell
python test_path_generator.py --model efsm_model.json --strategy both --max-depth 8 --max-paths 200 --include-negative --context test_path_context.bluemoon.json --output test_paths.json
```

Mã thoát: `0` thành công, `2` lỗi đầu vào (in `ERROR: ...`, không traceback). Không có đường dẫn Windows cố định.

## 9. Tái sử dụng cho workflow khác

Module chỉ cần: `states[].id`, `initial_state` (hoặc state `type: initial`), `transitions[].id/source/target`; còn lại là tùy chọn (guard/actions/rule_refs/expected_http_status/api, `variables[].origin`, `negative_transitions`, `invariants`). Không có tên state/transition/biến của BlueMoon trong mã; dữ liệu riêng của BlueMoon nằm trong file context (`test_path_context.bluemoon.json`). Các test A–G chạy trên một EFSM nhỏ không liên quan BlueMoon.
