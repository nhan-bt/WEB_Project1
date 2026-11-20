# Báo cáo kiểm chứng API Test Executor (Phần 6)

Số liệu dưới đây lấy từ lần chạy thật trên `test_cases.json` và từ pytest thật.

## 1. Kết quả chạy `test_cases.json`
| Chỉ số | Giá trị |
|---|---|
| Mục đầu vào (test case + path không chuyển đổi được) | 171 |
| Test case đã thực thi (`COMPLETED`) | 122 (102 positive, 20 negative) |
| Bỏ qua (`SKIPPED`) | 49, tất cả do `NO_FEASIBLE_DATA_FOUND` (path BLOCKED ở Phần 5, không chạy) |
| `EXECUTION_ERROR` / `PARTIAL` | 0 / 0 |
| Request đã gửi | 693 (fixture áp dụng: 1, ở TC106) |
| Lớp HTTP | 2xx = 673, 4xx = 20, 5xx = 0 |
| Mã HTTP | 200: 565, 201: 108, 400: 6, 404: 3, 409: 8, 422: 3 |
| Lỗi kỹ thuật | 0 |

3 test case `PARTIALLY_READY` (TC105, TC106, TC121) đều được thực thi. 20 phản hồi 4xx đều thuộc negative case và được ghi nhận như phản hồi nghiệp vụ, không phải lỗi kỹ thuật.
`COMPLETED` không có nghĩa là PASS; executor không đánh giá đúng/sai.

## 2. Kết quả pytest (chạy thật)
- `python -m pytest tests/test_api_test_executor.py`: **88 passed**.
- `python -m pytest` (toàn bộ): **586 passed** (498 test Phần 1–5 + 88 test Phần 6). Phần 1–5 không bị hồi quy.

Nhóm test: đọc test case, schema không hợp lệ, ánh xạ transition→API (693 bước khớp EFSM và route backend), `ExecutionContext`/`$ref`, chuỗi nhiều bước, truyền order id từ T04 (kiểm id thật khác 1), phản hồi 2xx/4xx/5xx/422/non-JSON, timeout và lỗi kết nối thật (socket loopback), bỏ qua BLOCKED/unconverted, cô lập DB (không rò trạng thái giữa các case, từ chối `online_shop.db`, phát hiện lệch môi trường), fixture (áp dụng và từ chối bảng ngoài allowlist), observed state, JSON kết quả hợp lệ, không có khóa PASS/FAIL, che dữ liệu nhạy cảm, CLI, chế độ external qua uvicorn thật.
Integration dùng FastAPI TestClient thật, không mock backend; mock chỉ dùng cho unit test logic thuần (transport giả).

Kịch bản positive đã chạy: thêm sản phẩm hợp lệ, cập nhật số lượng, tạo đơn, xác nhận, thanh toán thành công, thanh toán thất bại rồi thử lại, hủy đơn pending, hủy đơn đã xác nhận.
Kịch bản negative (NT thật của EFSM): vượt tồn kho (NT02), tạo đơn giỏ rỗng (NT03), thanh toán trước khi xác nhận (NT06), thanh toán lại (NT07), hủy đơn đã thanh toán (NT08), hủy đơn đã hủy (NT09), xác nhận đơn đã hủy (NT10), thanh toán đơn đã hủy (NT11).

## 3. An toàn dữ liệu
- `online_shop.db` giống hệt bản gốc sau khi chạy toàn bộ (so sánh `cmp`); `app/`, `efsm_model.json`, `guard_evaluator.py`, `test_path_generator.py`, `test_data_generator.py`, `test_cases.json`, `api_test_mapping.json` không đổi.
- Không tìm thấy giá trị `customer_name`/`phone`/`address` thật trong file kết quả.

## 4. Giới hạn
- 49 path BLOCKED ở Phần 5 không được chạy; đây là giới hạn của dữ liệu Phần 5, chưa phải lỗi executor.
- Fixture chỉ hỗ trợ `products.stock` (allowlist), chỉ ở chế độ in-process. Chế độ external không reset và không áp fixture nên không bảo đảm cô lập (`NOT_GUARANTEED`).
- Kết quả không tái lập từng byte (thời gian chạy, `created_at` trong response).
- Backend dùng chung một giỏ hàng, nên các case chạy tuần tự, không song song.
- Executor không kiểm tra expected, nên chưa có kết luận đúng/sai hay lỗ hổng nào.

## 5. Sẵn sàng cho Phần 7
Sẵn sàng: mỗi bước có `expected_ref` trỏ về `test_cases.json`, có response đầy đủ (đã che dữ liệu nhạy cảm), observed state sau từng bước và assertion probes. Phần 7 chỉ cần đọc `test_execution_results.json` và `test_cases.json`.
