# Nghiệm thu `test_data_generator.py` (phần lõi)

Tất cả số liệu dưới đây lấy từ lần chạy thực tế (Python 3.12.3, thư mục gốc `WEB_Project1`).

## 1. Kết quả kiểm thử

| Lệnh | Kết quả |
|---|---|
| `python -m py_compile test_data_generator.py` | OK |
| `python -m pytest tests/test_test_data_generator.py -v` | **82 passed**, 0 failed, 0 skipped |
| `python -m pytest -v` (toàn bộ) | **432 passed**, 0 failed, 0 skipped, 1 warning |
| `pytest -q` (không `-m`) và `-W error::pytest.PytestCollectionWarning` | 432 passed |

Cảnh báo duy nhất (`StarletteDeprecationWarning` về `httpx`) đã có từ trước. Số test theo file: `test_api.py` 5 · `test_efsm_backend_conformance.py` 33 · `test_guard_evaluator.py` 174 · `test_test_path_generator.py` 138 · **`test_test_data_generator.py` 82** (= 432).

### Phân bổ 82 test mới

| Nhóm | Nội dung | Số test |
|---|---|---|
| Phân tích biến | vai trò input/context, phụ thuộc giữa biến, `stock` không phải hằng số, miền chỉ lấy từ EFSM, phi tuyến, guard không hỗ trợ, không có guard, invariant cần dữ liệu runtime | 12 |
| EP / BVA | valid/invalid partition, **bảng biên của đề bài** (`-1, 0, 1, 9, 10, 11`) được suy từ guard, biên dời theo `stock`/`current_qty`/toán tử, đoạn phân hoạch chính xác, đại diện nằm trong đoạn | 10 |
| AND / OR / NOT, nhiều biến, enum | vi phạm đúng một điều kiện, OR, NOT, `x + z == 10 ∧ x > z` giải đồng thời, ràng buộc input–context, phi tuyến, enum/boolean/chuỗi, mỗi điều kiện có mẫu vi phạm đơn, `single_condition` không bao giờ được giả định | 10 |
| Thiếu / sai kiểu | thiếu biến, `None`, sai kiểu theo kiểu khai báo, tắt từng loại mẫu | 6 |
| UNSAT / UNKNOWN / UNSUPPORTED | UNSAT bằng khoảng số nguyên, UNSAT bằng miền hữu hạn, UNSAT kèm giả định, **hết ngân sách / hết ứng viên ⇒ UNKNOWN**, vô nghiệm nhưng không chứng minh được ⇒ UNKNOWN, DNF quá lớn, UNSUPPORTED, tìm vi phạm | 15 |
| Đối chiếu GuardEvaluator | mọi mẫu của guard được hỗ trợ đều VERIFIED, trạng thái khớp verdict của evaluator, **evaluator "nói dối" ⇒ UNVERIFIED** (3 biến thể), transition qua `check_transition`, không guard, thả lỏng ngữ cảnh | 9 |
| Output / ổn định / bất biến đầu vào | output giống hệt từng byte, `sample_id` liên tục, schema, tổng kết nhất quán, **không sửa model/guard/context**, chỉ chứa biến của guard, CLI | 9 |
| Integration BlueMoon | 9/9 guard được hỗ trợ và mọi mẫu VERIFIED, biên T01/T02/T04 từ biểu thức thật, mỗi điều kiện của T04 có mẫu vi phạm đơn, enum, T06/T07 bù nhau, hash model, chạy không cần context | 11 |

Các test A–… dùng một EFSM nhỏ (20 guard) độc lập với BlueMoon; nhóm cuối dùng `efsm_model.json` thật.

### Độ nhạy của test (kiểm tra đột biến thủ công)
Mình cố ý làm hỏng generator theo 13 cách: lệch biên `<=` / `>=`, kết luận UNSAT khi hết ngân sách, kết luận UNSAT khi hết ứng viên, luôn gắn VERIFIED, không dùng verdict của evaluator cho mẫu không hợp lệ, bỏ hạ bậc khi dự đoán ≠ evaluator, mặc định `single_condition = true`, bỏ mẫu thiếu biến, bỏ ngữ cảnh cố định, bỏ chứng minh khoảng, bỏ giá trị ngoài miền, sửa đầu vào.
Lần đầu **5/13 sống sót** (hai lệch biên, hai lớp xác minh che nhau, `single_condition`); mình bổ sung 4 test (đoạn phân hoạch chính xác, evaluator nói dối từng phần, guard `x > 0 ∧ x > 5`) và chạy lại: **13/13 bị phát hiện**. Đây là kiểm tra thủ công, không phải mutation testing đầy đủ.

### Lỗi gặp trong quá trình làm (không sửa kỳ vọng để test đạt)
Lần chạy đầu có 10 test thất bại; phân loại:
* **Cải tiến generator (3):** lý do UNSAT cụ thể (khoảng số nguyên / duyệt hết miền hữu hạn) bị thay bằng thông điệp chung → giờ được giữ trong `reason`.
* **Khả năng bộ giải (2):** `x * z == 12 ∧ x > 1 ∧ z > 1` không tìm được nghiệm vì `6` và `4` không nằm trong tập ứng viên → thêm **ước số của hằng** vào ứng viên.
* **Kỳ vọng sai của test (5):** helper `values()` lấy nhầm mẫu mà biến *khác* bị đổi (2 test BlueMoon); thông điệp UNSUPPORTED của `SUM_PRODUCT` nói về hàm chứ không phải kiểu `list`; guard "ngoài tập ứng viên" (`x * z == 1000003`) thực ra giải được vì chính hằng số nằm trong ứng viên → đổi sang `x * z == 3000009`, và thêm ca số nguyên tố `1000003` (vô nghiệm nhưng không chứng minh được ⇒ UNKNOWN); tổng kết `unsolved` cập nhật theo.

## 2. Kết quả trên EFSM BlueMoon

Lệnh: `python test_data_generator.py --model efsm_model.json --context test_data_context.bluemoon.json --output generated_guard_data.json` (chạy hai lần cho file **giống hệt từng byte**).

| Chỉ số | Giá trị |
|---|---|
| Transition | 10 |
| **Tổng số guard đã phân tích** | **9** (T10 không có guard) |
| Guard được hỗ trợ | **9** |
| Guard chưa hỗ trợ | **0** |
| Tổng số mẫu | **119** |
| Mẫu hợp lệ (`expected_guard = true`) | **40** |
| Mẫu không hợp lệ (`expected_guard = false`) | **55** |
| Mẫu sai định dạng (thiếu / null / sai kiểu) | **24** |
| `VERIFIED_VALID` / `VERIFIED_INVALID` | 40 / 79 (= 55 + 24) |
| `UNVERIFIED` / `UNSUPPORTED` | **0 / 0** |
| **Mẫu đã được GuardEvaluator xác minh** | **119 / 119** |
| Mẫu không hợp lệ được xác nhận chỉ vi phạm **đúng một** điều kiện | 55 / 55 |
| Ràng buộc chưa giải được (`unsolved`) | không có |

Phân bổ theo transition (hợp lệ / không hợp lệ / sai định dạng): T01 5/5/3 · T02 5/6/3 · T03 1/1/0 · T10 1/0/0 (không guard) · T04 21/21/12 · T05 1/4/0 · T06 1/6/3 · T07 1/6/3 · T08 2/3/0 · T09 2/3/0.
Chạy **không** có file context: 106 mẫu (27 hợp lệ, 55 không hợp lệ, 24 sai định dạng), 106/106 được xác minh; số mẫu hợp lệ ít hơn vì không có context nên bộ giải chọn nghiệm đầu tiên tìm được (`stock = 1`, `quantity = 1`), khoảng hợp lệ của `quantity` chỉ còn `[1, 1]` và có ít giá trị biên hợp lệ hơn (T01: 1 so với 5, T02: 1 so với 5, T04: 16 so với 21). Nên cung cấp context (như `test_data_context.bluemoon.json`) để có khoảng hợp lệ rộng hơn.

Một số điểm cụ thể:
* **T01** với `stock = 10`, `current_qty = 0`: `quantity ∈ {-1, 0, 1, 2, 5, 9, 10, 11, 12}`; `0` vi phạm đúng `quantity > 0`, `11` vi phạm đúng `current_qty + quantity <= stock`.
* **T04**: cả 10 điều kiện đều có ít nhất một mẫu vi phạm đơn đã được xác minh (kể cả `ordered_quantity <= stock`, vi phạm bằng cách tăng `ordered_quantity` hoặc giảm `stock`).
* **T06/T07**: `SUCCESS` và `FAILED` bù nhau; giá trị ngoài miền `__OUT_OF_DOMAIN__` được kiểm tra ở cả `order_status` và `payment_result`.
* **Invariant** (`invariant_analysis`): `INV-03` **UNSUPPORTED** (`SUM_PRODUCT` trên danh sách `order_items`); `INV-06/07/08/10` không áp dụng (kiểm tra cấu trúc mô hình); 7 invariant còn lại SUPPORTED ở mức phân tích (chưa sinh dữ liệu cho invariant).

### Ràng buộc chưa giải được (kiểm chứng trên EFSM nhỏ)
Trong guard BlueMoon không có ràng buộc nào chưa giải được. Các trường hợp giới hạn được chứng minh bằng fixture: `x > 5 ∧ x < 3` ⇒ `UNSAT` (khoảng); `flag == true ∧ flag == false`, `mode == "A" ∧ mode == "B"` ⇒ `UNSAT` (miền hữu hạn); `x * x == 2` và `x * z == 1000003` (vô nghiệm nhưng không chứng minh được) ⇒ `UNKNOWN`; `x * z == 3000009` (có nghiệm `3 × 1000003` ngoài ứng viên) ⇒ `UNKNOWN`; `exists`, `SUM_PRODUCT` ⇒ `UNSUPPORTED`.

## 3. Phạm vi và các file không đổi

So với cây thư mục trước khi làm phần này, **giống từng byte**: `test_path_generator.py`, `test_paths.json`, `test_path_coverage.json`, `efsm_model.json`, `guard_evaluator.py`, `security_rules.json`, `online_shop.db`, `app/`, `requirements.txt`, `conftest.py`, `tests/test_api.py`, `tests/test_guard_evaluator.py`, `tests/test_efsm_backend_conformance.py`, `tests/test_test_path_generator.py`, `test_path_context.bluemoon.json`.
Không gửi HTTP, không sinh dữ liệu cho `test_paths.json`, không có API executor hay result validator.

## 4. Giới hạn chưa giải quyết

1. **Miền số chưa được xác định trong EFSM** (không có min/max). Dữ liệu chỉ phản ánh các biên suy từ guard; không có kiểm tra "giá trị quá lớn/nhỏ về mặt nghiệp vụ".
2. **UNSAT chỉ chứng minh được trong hai trường hợp** (khoảng một biến, miền hữu hạn khai báo). Ràng buộc nhiều biến/phi tuyến vô nghiệm sẽ báo `UNKNOWN`, và ràng buộc phi tuyến có nghiệm ngoài tập ứng viên cũng `UNKNOWN`.
3. **Tìm kiếm có ngân sách** (`max_nodes = 20000` mỗi thành phần) và ứng viên cố định (`int_pool`); tập ứng viên không phải miền nghiệp vụ.
4. **`decimal`, `list`, `exists`, `not_empty`, `SUM_PRODUCT`** chưa được hỗ trợ ở bộ sinh (GuardEvaluator vẫn đánh giá được chúng).
5. **Một biến mỗi lần**: không sinh tổ hợp nhiều biến cùng vi phạm; chỉ có tìm kiếm bổ sung khi một điều kiện chưa có mẫu vi phạm đơn.
6. **Giá trị biến context là do người dùng cấp** (`test_data_context.bluemoon.json` là ví dụ do mình viết, không phải dữ liệu thật của backend); module không đọc database.
7. **Chưa nhất quán xuyên bước**: dữ liệu mức một guard; liên kết giữa các transition (ví dụ `quantity` ở T01 với `ordered_quantity` / `order_items` ở T04) thuộc phần 2.
8. Mẫu sai định dạng chỉ sinh cho biến `input` (client có thể gửi thiếu/sai kiểu); biến `database`/`derived` không có mẫu loại này.

## 5. Chạy trên Windows (PowerShell, thư mục gốc `WEB_Project1`)

```powershell
python -m py_compile test_data_generator.py
python -m pytest tests/test_test_data_generator.py -v
python -m pytest -v
python test_data_generator.py --model efsm_model.json --context test_data_context.bluemoon.json --output generated_guard_data.json
```

`test_data_generator.py` nằm ở thư mục gốc nên pytest có thể thu thập nó như một module test; lớp `TestDataGenerator` đã đặt `__test__ = False` nên không phát sinh cảnh báo. Cần Python ≥ 3.11; không cần thư viện ngoài `requirements.txt`.
