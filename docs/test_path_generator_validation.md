# Nghiệm thu `test_path_generator.py`

## 1. Kết quả kiểm thử thực tế

Chạy trên Python 3.12.3, thư mục gốc `WEB_Project1` đã tích hợp (xem mục 6).

| Lệnh | Kết quả |
|---|---|
| `python -m py_compile test_path_generator.py` | OK |
| `python -m pytest tests/test_test_path_generator.py -v` | **138 passed**, 0 failed, 0 skipped |
| `python -m pytest -v` (toàn bộ) | **350 passed**, 0 failed, 0 skipped, 1 warning |
| `pytest -q` (không `-m`, nhờ `conftest.py`) | 350 passed |
| `pytest -W error::pytest.PytestCollectionWarning` | 350 passed (không có cảnh báo thu thập từ lớp `TestPathGenerator`) |

Cảnh báo duy nhất (`StarletteDeprecationWarning` về `httpx`) đã có từ trước, không liên quan đến thay đổi này.

Số test theo từng file: `test_api.py` 5 · `test_efsm_backend_conformance.py` 33 · `test_guard_evaluator.py` 174 · `test_test_path_generator.py` **138** (= 5 + 33 + 174 + 138 = 350).

### Phân bổ 138 test mới

| Nhóm | Nội dung | Số test |
|---|---|---|
| A. Graph construction | cạnh, self-loop, cạnh song song, unreachable, dead-end, chu trình, negative không vào đồ thị, lỗi mô hình | 13 |
| B. BFS | ngắn nhất, bắt đầu từ initial, nối tiếp, ổn định, prune tiền tố, `max_depth`, `max_paths`, prefix khả thi | 14 |
| C. DFS | nhiều path, dừng với cycle/self-loop, `max_depth`/`max_paths`/visits/repeats, tối đại, ổn định, giới hạn sai | 19 |
| D. Negative path | negative ở cuối, prefix dương, không negative trong prefix, nhãn phân loại, expected rejection, HTTP status không tự đặt, bỏ qua source không đạt tới | 19 |
| E. Feasibility | thỏa/không thỏa guard, thiếu context/biến, action không mô phỏng được, dữ liệu từ bước trước, request không mang sang bước sau, invariant vi phạm, `strict`, negative, explore | 28 |
| F. Coverage | tính đúng state/transition/negative/cặp, không báo 100% khi còn thiếu, UNKNOWN không tính là feasible, executed = NOT_MEASURED | 12 |
| G. Output/CLI | JSON hợp lệ, ID duy nhất, transition tồn tại, state khớp transition, metadata negative, ổn định từng byte, không timestamp, tham chiếu catalog, CLI | 19 |
| Integration BlueMoon | đồ thị thật, prefix khả thi thêm `T01`, 20 negative đều có path, rule không còn unlinked, ổn định | 14 |

(Nhóm A–G dùng EFSM nhỏ tự dựng, độc lập với BlueMoon; cột "Số test" tính theo số test thu thập được, gồm cả tham số hóa.)

### Độ nhạy của test
Mình cố ý làm hỏng generator theo 10 cách (guard sai coi là OK; thiếu biến thành INFEASIBLE; DFS bỏ qua `max_depth`; đưa negative vào đồ thị; mang biến request sang bước sau; đếm feasible coverage từ mọi path; executed coverage ≠ NOT_MEASURED; DFS bỏ self-loop; tắt prune tiền tố; bỏ qua vi phạm invariant). **Cả 10 đột biến đều bị test phát hiện.** Đây là kiểm tra thủ công, không phải mutation testing đầy đủ.

### Phát hiện trong quá trình kiểm thử (không sửa expected để test đạt)
1. Một integration test thất bại (`NT03`): khi context thiếu biến cho các điều kiện khác của guard `T04`, generator không chỉ ra được điều kiện nào bị vi phạm. Đây là **thiếu sót thật của generator**; đã sửa generator (đánh giá từng nhánh của `all`, chỉ cần một nhánh sai là đủ kết luận) chứ không sửa test.
2. File `test_paths.json` ban đầu 1,9 MB vì mỗi bước lặp lại guard/effects/api; đã chuyển sang tham chiếu `transition_catalog` bằng ID (còn 1,34 MB), giả định chung ghi một lần ở `feasibility_assumptions`.
3. Dự án gốc từng chứa EFSM/evaluator **v1 cũ**; xây dựng trên bản v2, hiện đã hợp nhất vào cây thư mục dự án.

## 2. Kết quả chạy trên EFSM BlueMoon

Lệnh: `python test_path_generator.py --model efsm_model.json --strategy both --max-depth 8 --max-paths 200 --include-negative --context test_path_context.bluemoon.json --output test_paths.json`. Chạy hai lần cho **file giống hệt từng byte** (md5 trùng).

| Chỉ số | Giá trị |
|---|---|
| Tổng path | **171** = 151 positive + 20 negative |
| Positive theo chiến lược | BFS-only 4 · BFS∩DFS 3 · DFS-only 144 (147 path DFS, 3 trùng BFS được gộp) |
| Positive theo feasibility | FEASIBLE 32 · INFEASIBLE 119 · UNKNOWN 0 |
| Negative theo feasibility | FEASIBLE 20 |
| DFS | 147 path, đều kết thúc ở `TERMINAL_STATE`, dài nhất 7 transition (≤ `max_depth` 8); không chạm giới hạn |
| Giới hạn bị chạm (`limits_hit`) | không có |
| Warnings | không có |

119 path INFEASIBLE đều do `GUARD_NOT_SATISFIED` với context giỏ rỗng — đúng bản chất "hợp lệ cấu trúc ≠ thực thi được" (ví dụ path mở đầu bằng `T04` trên giỏ rỗng, hoặc `T01>T03>T02` khi dòng giỏ đã bị xóa).

Coverage (`test_path_coverage.json`):

| | Cấu trúc | Khả thi (trên mô phỏng) | Thực thi |
|---|---|---|---|
| State | 5/5 | 5/5 | NOT_MEASURED |
| Positive transition | 10/10 | 10/10 | NOT_MEASURED |
| Cặp transition liên tiếp | 28/28 | 14/28 | NOT_MEASURED |
| Negative transition | 20/20 có path | 20/20 | NOT_MEASURED |
| Rule IMPLEMENTED | 26/26 được liên kết với path | 26/26 thuộc ≥1 path FEASIBLE | `executed_on_backend`, `verified_by_oracle`, `with_generated_test_data`: NOT_MEASURED |

Ba rule `NOT_APPLICABLE` (`BSR-AUTH-01`, `BSR-CANCEL-COMPLETED`, `BSR-PAYMENT-EXTERNAL`) được liệt kê riêng và **không** tính là đã bao phủ. Chạy **không** có context (`--strategy bfs --include-negative`): 28 path (8 positive + 20 negative), **toàn bộ UNKNOWN**; coverage cấu trúc 5/5 state, 10/10 transition, 20/20 negative nhưng coverage khả thi = 0.

Phân loại 20 negative path (nhãn chính): INVALID_STATE_TRANSITION 8 · BUSINESS_CONSTRAINT_VIOLATION 7 · INPUT_VALIDATION_VIOLATION 5; 12 path có nhiều nhãn. Độ dài prefix: 11 path prefix rỗng, 1 path dài 2, 5 path dài 3, 3 path dài 4.

## 3. Ví dụ

**Positive** `P005` — `T01>T04>T05>T06` (CART → CART → PENDING_CONFIRMATION → CONFIRMED → PAID), strategy BFS (đồng thời DFS), goals: state `PAID`, transition `T06`, `FEASIBLE`. Vì sao: đường đi cấu trúc ngắn nhất tới `PAID` là `T04>T05>T06`, nhưng guard `T04` cần `cart_items_count > 0`; với giỏ rỗng nó INFEASIBLE (`cart_items_count > 0 is false (0 > 0)`). BFS trên (state, biến) tìm ra rằng cần thêm `T01` (self-loop ở `CART`, gán `cart_items_count := 1`) trước `T04`. Các bước sau chỉ khả thi vì `T04` gán `order_status`, `payment_count`, `order_total`…; `required_context` cho biết cần `quantity` ở bước 1, `payment_method`/độ dài chuỗi ở bước 2, `payment_result` ở bước 4.

**Negative** `N008` — `T01>T04>T05>T06>!NT08`: prefix 4 bước đưa đơn tới `PAID`, rồi `cancel_order` bị cấm. Nhãn `INVALID_STATE_TRANSITION` vì `cancel_order` chỉ có positive từ `PENDING_CONFIRMATION`/`CONFIRMED` (T08, T09), không có từ `PAID`. `expected_rejection.http_status = 409` lấy từ `negative_transitions[NT08].expected.http_status`; `forbidden_backend_effects` là effect của `T08` (hoàn kho, đổi status → CANCELLED) không được xảy ra; rule `BSR-CANCEL-01`, `BSR-CANCEL-02`, severity `HIGH`, mutant `BUG-08`; `expected_state_unchanged = true`. `feasibility = FEASIBLE` (`PREFIX_FEASIBLE_NEGATIVE_APPLICABLE`): prefix khả thi và điều kiện `order_status == PAID` đúng.

## 4. Giới hạn hiện tại

1. **FEASIBLE ≠ đã thực thi.** Nó chỉ nói rằng chuỗi khả thi trên mô phỏng biến EFSM với *một* bộ context. Chưa có API nào được gọi; coverage thực thi/oracle là `NOT_MEASURED`.
2. **`INFEASIBLE` chỉ đúng với context đã cung cấp** (`proof_scope = GIVEN_CONTEXT`), không phải chứng minh phổ quát; không có SMT solver. Một path INFEASIBLE với context này có thể khả thi với dữ liệu khác.
3. **Chính sách `effects_policy` mặc định `model_contract`** giả định mọi biến EFSM mà backend thay đổi đều có `assign` tương ứng (đã được conformance test của giai đoạn trước kiểm chứng cho các hành vi chính). Nếu không tin hợp đồng này, dùng `--effects-policy strict` (khi đó mọi path có effect tối đa UNKNOWN — với BlueMoon là toàn bộ).
4. **Context mẫu do mình viết** (`test_path_context.bluemoon.json`): một giá trị cho mỗi transition, **không phải** dữ liệu từ `test_data_generator.py`. Nó đặt giá trị tay nhất quán (ví dụ `T01.quantity = 2` với `T04.ordered_quantity = 2`, `order_items` một dòng); mô hình không liên kết các biến này (REPRESENTATION_GAP).
5. **Một bộ đầu vào cho mỗi transition** (hoặc theo số bước); chưa tìm kiếm trên nhiều giá trị cho cùng transition.
6. **DFS lấy N path đầu theo thứ tự duyệt** nên thiên về self-loop đầu mô hình khi `max_paths` nhỏ; không có hướng dẫn theo coverage. DFS chỉ phát path tối đại (không phát các tiền tố).
7. **Đơn một dòng hàng:** `ordered_quantity`, `stock` là mức dòng hàng; đường đi nhiều dòng chỉ được thể hiện qua `order_items` (INV-03).
8. **Phân loại negative dựa trên `origin` của biến và HTTP status** của mô hình; nếu mô hình thiếu `origin`, chỉ có nhãn INVALID_STATE_TRANSITION/GUARD_VIOLATION. `GUARD_VIOLATION` không bao giờ là nhãn chính vì luôn đi kèm nhãn cụ thể hơn.
9. **`NT04` được gắn FEASIBLE ở mức mô hình nhưng `api_reachable = false`**: không thực thi được qua API (cần seed DB hoặc mutant BUG-04). Tương tự BUG-10 không có negative qua API.
10. **Transition lỗi 404 do `order_id` không tồn tại** không có trong mô hình nên không có path.
11. Negative path chỉ có **một** bước negative ở cuối (đúng yêu cầu phiên bản đầu); chưa có chuỗi nhiều lỗi.
12. Độ bao phủ cặp transition khả thi chỉ 14/28. 14 cặp chưa có path FEASIBLE nào bao phủ, vì hai nguyên nhân khác nhau: (a) **giới hạn DFS** (`max_visits_per_state=3` chặn các chuỗi self-loop dài ở `CART`, ví dụ `T01>T10`, `T02>T03`, `T03>T01`, `T10>T02`); (b) **guard thực sự không thỏa** với context giỏ rỗng sau khi xóa/làm trống giỏ (`T03>T04`, `T10>T04`, `T03>T02`, `T10>T03`…). Đã thử `--max-visits-per-state 5 --max-transition-repeats 3 --max-depth 10 --max-paths 2000`: 2024 path, cặp khả thi tăng lên 22/28; 6 cặp còn lại thuộc nhóm (b).

## 5. Điều `test_data_generator.py` cần giải quyết

* Sinh **nhiều** bộ giá trị cho `required_context` (`initial_variables`, `step_inputs` theo số bước, `negative_inputs`); biên giá trị (0, âm, bằng tồn kho, vượt tồn kho…).
* Biến `origin = database` (tồn kho, đơn giá, giỏ) phải được **chuẩn bị trong DB/bằng các bước setup** trước khi chạy; `origin = derived` (`ordered_quantity`, `order_items`, `payment_count`…) phải khớp với dữ liệu thực tế sau mỗi bước (hiện chỉ nhất quán nhờ context tay).
* Chuyển `*_length` thành chuỗi có độ dài tương ứng; ánh xạ biến → trường request qua `variables[].api_field` và `transition_catalog.*.api`.
* Dùng `witness` của path FEASIBLE làm điểm xuất phát; với `INFEASIBLE`/`UNKNOWN` phải tìm bộ giá trị khác hoặc coi là chưa kiểm thử được.
* Với negative: mỗi path chỉ vi phạm **một** điều kiện (`invalid_condition`); thứ tự kiểm tra của backend (Pydantic 422 → 404 → 409/400) quyết định mã lỗi nếu vi phạm nhiều điều kiện cùng lúc.
* Oracle: dùng `expected_rejection` (`http_status`, `forbidden_backend_effects`, `expected_state_unchanged`) và `transition_catalog.*.unsimulated_effects` để kiểm tra tác động trên DB.
* Cập nhật `with_generated_test_data`, `executed_on_backend`, `verified_by_oracle` — hiện đều `NOT_MEASURED`.

## 6. Cấu trúc dự án và chạy trên Windows

```
WEB_Project1/
├── efsm_model.json                    mô hình EFSM v2
├── security_rules.json                danh mục Business Security Rules (không đổi)
├── guard_evaluator.py                 bộ đánh giá guard/invariant
├── test_path_generator.py             bộ sinh đường đi kiểm thử
├── test_paths.json                    kết quả sinh đường đi cho BlueMoon
├── test_path_coverage.json            báo cáo coverage
├── test_path_context.bluemoon.json    context mẫu (đầu vào tùy chọn của generator, cần cho integration test)
├── conftest.py                        để `pytest` (không -m) import được guard_evaluator/app
├── tests/   test_api.py, test_guard_evaluator.py, test_efsm_backend_conformance.py, test_test_path_generator.py
├── docs/    efsm_backend_mapping.md, efsm_validation_report.md, test_path_generator_design.md, test_path_generator_validation.md
├── app/                               backend (không đổi)
├── online_shop.db                     database demo (không đổi)
├── requirements.txt, README.md
```

```powershell
cd WEB_Project1
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m py_compile test_path_generator.py
python -m pytest tests/test_test_path_generator.py -v
python -m pytest -v
python test_path_generator.py --model efsm_model.json --strategy both --max-depth 8 --max-paths 200 --include-negative --context test_path_context.bluemoon.json --output test_paths.json
```

Chạy `pytest` từ thư mục gốc `WEB_Project1` (giống `test_api.py`, vốn dùng `test_online_shop.db` tương đối theo thư mục chạy). Generator không đọc/ghi `online_shop.db`. Cần Python ≥ 3.11. Không cần thêm thư viện ngoài `requirements.txt`.

`test_path_generator.py` nằm ở thư mục gốc nên pytest có thể thu thập nó như một module test; lớp `TestPathGenerator` đã đặt `__test__ = False` nên không phát sinh cảnh báo.
