# Đối chiếu EFSM ↔ backend BlueMoon

Tài liệu này được lập từ **đọc trực tiếp source code** (`app/service/cart.py`, `order.py`, `payment.py`, `app/schemas.py`,
`app/models.py`, `app/routes/*.py`, `app/database.py`) và được **kiểm chứng bằng chạy thật** bởi
`tests/test_efsm_backend_conformance.py` (33 ca, chạy trên database test riêng). Backend không bị sửa.

Phân loại sai lệch: `MODEL_ERROR` (EFSM mô tả sai) · `IMPLEMENTATION_DEFECT` (backend vi phạm rule đã xác nhận) ·
`REQUIREMENT_AMBIGUITY` (chưa đủ thông tin) · `REPRESENTATION_GAP` (hành vi đúng nhưng mô hình chưa biểu diễn).

## 1. Trạng thái thực sự được backend sử dụng

`orders.status` chỉ nhận 4 giá trị: `PENDING_CONFIRMATION`, `CONFIRMED`, `PAID`, `CANCELLED` (gán trong `order.py`/`payment.py`).
Giỏ hàng **không có cột trạng thái**; `CART` là state mức mô hình cho vòng đời giỏ. Vì vậy `efsm_model.json` v2 gắn
`entity_type`/`lifecycle` cho từng state: `CART` → `cart`; 4 state còn lại → `order` (và `order_status_value` trùng id).
Không thêm state mới vì backend không có `COMPLETED`, `SHIPPED`...

## 2. Bảng đối chiếu transition

| Transition | Source State | Event/API | Guard thực tế (backend) | Action thực tế | Target State | HTTP Result |
|---|---|---|---|---|---|---|
| **T01** | CART | `POST /api/cart/items` (`cart.add_cart_item`) | Pydantic: `product_id>0`, `quantity>0` (422); product tồn tại (404); `current_qty+quantity <= stock` (400) | UPSERT `cart_items.quantity = current+quantity` | CART | 200 / 422 / 404 / 400 |
| **T02** | CART | `PATCH /api/cart/items/{pid}` (`cart.update_cart_item`) | Pydantic `quantity>0` (422); product tồn tại (404); dòng giỏ tồn tại (404); `quantity <= stock` (400) | `cart_items.quantity = quantity` | CART | 200 / 422 / 404 / 400 |
| **T03** | CART | `DELETE /api/cart/items/{pid}` (`cart.delete_cart_item`) | dòng giỏ tồn tại (404). Không kiểm tra product | DELETE dòng `cart_items` | CART | 200 / 404 |
| **T10** | CART | `DELETE /api/cart` (`cart.clear_cart`) | không có (luôn thành công, kể cả giỏ rỗng) | DELETE toàn bộ `cart_items` | CART | 200 |
| **T04** | CART | `POST /api/orders` (`order.create_order`) | Pydantic: tên 2..100, phone 9..15, address 5..255 (422); giỏ không rỗng (400); `payment_method ∈ {MOCK,COD}` (400); mỗi dòng `0 < qty <= stock` (400) | tạo `orders(PENDING_CONFIRMATION)` + `order_items`(giá từ `products.price`); `stock -= qty`; xóa giỏ — một transaction | PENDING_CONFIRMATION | 201 / 422 / 400 |
| **T05** | PENDING_CONFIRMATION | `POST /api/orders/{id}/confirm` (`order.confirm_order`) | đơn tồn tại (404); `status == PENDING_CONFIRMATION` (409) | `status = CONFIRMED` | CONFIRMED | 200 / 404 / 409 |
| **T06** | CONFIRMED | `POST /api/orders/{id}/pay` (`payment.pay_order`) | đơn tồn tại (404); `status == CONFIRMED` (409); `simulate_result ∈ {SUCCESS,FAILED}` (400, kiểm tra SAU status) | INSERT `payments(amount=order.total_amount, SUCCESS)`; `status = PAID` | PAID | 200 / 404 / 409 / 400 |
| **T07** | CONFIRMED | như T06 với `simulate_result=FAILED` | như T06 | INSERT `payments(..., FAILED)`; `status` giữ CONFIRMED; có thể thử lại | CONFIRMED | 200 |
| **T08** | PENDING_CONFIRMATION | `POST /api/orders/{id}/cancel` (`order.cancel_order`) | đơn tồn tại (404); `status ∈ {PENDING_CONFIRMATION, CONFIRMED}` (409) | `stock += qty` mỗi dòng; `status = CANCELLED` — một transaction | CANCELLED | 200 / 404 / 409 |
| **T09** | CONFIRMED | như T08 | như T08 | như T08 | CANCELLED | 200 / 404 / 409 |

Thứ tự kiểm tra ảnh hưởng mã lỗi: **Pydantic (422) chạy trước service**; trong `create_order`, giỏ rỗng (400) được kiểm tra trước
`payment_method` (400); trong `pay_order`, **status (409) được kiểm tra trước `simulate_result` (400)** — đơn chưa xác nhận kèm kết quả sai trả 409, không phải 400
(có test `test_status_check_precedes_result_validation_on_pay`).

Dữ liệu thay đổi và tồn kho: tồn kho chỉ bị **trừ khi tạo đơn** (T04) và **hoàn khi hủy** (T08/T09) trong cùng transaction với đổi status.
Thêm giỏ hàng **không** trừ kho. Thanh toán không đổi kho. `payments.amount` lấy từ `orders.total_amount`, không nhận từ client.
Phương thức thanh toán được chấp nhận khi tạo đơn: `MOCK`, `COD`. Thanh toán ghi nhận `SUCCESS` → đơn `PAID`; `FAILED` → payment được lưu, đơn giữ `CONFIRMED`.

## 3. Negative transitions

| ID | Source | Event | Điều kiện không hợp lệ | HTTP (model) | Rules | Mutant | Ghi chú |
|---|---|---|---|---|---|---|---|
| NT01 | CART | add_item | `quantity <= 0` | 422 | CART-01 | BUG-01 |  |
| NT02 | CART | add_item | `current_qty + quantity > stock` | 400 | CART-02 | BUG-02 |  |
| NT03 | CART | create_order | `cart_items_count == 0` | 400 | ORDER-01 | BUG-03 |  |
| NT04 | CART | create_order | `ordered_quantity > stock` | 400 | STOCK-01, STOCK-03 | BUG-04 | KHÔNG đạt được qua API. Không thể đạt được chỉ qua API (giỏ luôn <= stock lúc thêm; tạo đơn xóa giỏ). Cần seed DB hoặc mutant BUG-04. |
| NT05 | CONFIRMED | confirm_order | `order_status != PENDING_CONFIRMATION` | 409 | ORDER-05 | BUG-05 |  |
| NT06 | PENDING_CONFIRMATION | pay | `order_status != CONFIRMED` | 409 | PAYMENT-01, PAYMENT-04 | BUG-06 |  |
| NT07 | PAID | pay | `order_status == PAID` | 409 | PAYMENT-05 | BUG-07 |  |
| NT08 | PAID | cancel_order | `order_status == PAID` | 409 | CANCEL-01, CANCEL-02 | BUG-08 |  |
| NT09 | CANCELLED | cancel_order | `order_status == CANCELLED` | 409 | CANCEL-01, STOCK-05 | BUG-09 |  |
| NT10 | CANCELLED | confirm_order | `order_status == CANCELLED` | 409 | ORDER-05 | BUG-05 |  |
| NT11 | CANCELLED | pay | `order_status == CANCELLED` | 409 | PAYMENT-04 | BUG-06 |  |
| NT12 | CONFIRMED | pay | `payment_result not in {SUCCESS, FAILED}` | 400 | PAYMENT-07 | - | v1 ghi 422; backend thực tế trả 400 (không có ràng buộc Pydantic cho simulate_result) - khớp BSR-PAYMENT-07. |
| NT13 | CART | create_order | `payment_method not in {MOCK, COD}` | 400 | ORDER-02 | - | Điều kiện tiên quyết: giỏ không rỗng (kiểm tra giỏ rỗng chạy trước). |
| NT14 | CART | create_order | `customer_name_length < 2 or customer_name_length > 100 or phone_length < 9 or phone_length > 15 or address_length < 5 or address_length > 255` | 422 | ORDER-03 | - |  |
| NT15 | CART | update_quantity | `cart_item_exists == False` | 404 | CART-04 | - |  |
| NT16 | CART | update_quantity | `quantity > stock` | 400 | CART-02 | - |  |
| NT17 | CART | update_quantity | `quantity <= 0` | 422 | CART-01 | - |  |
| NT18 | CART | remove_item | `cart_item_exists == False` | 404 | CART-05 | - |  |
| NT19 | CART | add_item | `product_exists == False` | 404 | CART-03 | BUG-02A |  |
| NT20 | PAID | confirm_order | `order_status == PAID` | 409 | ORDER-05 | - |  |

## 4. Invariant (đã chuẩn hóa thành dạng máy đánh giá được)

| ID | Scope | Loại | Phạm vi áp dụng | Kiểm tra | Dữ liệu cần |
|---|---|---|---|---|---|
| INV-01 | CART | state_predicate | CART | `quantity > 0` | quantity |
| INV-02 | ORDER | state_predicate | CART, PENDING_CONFIRMATION, CONFIRMED, PAID | `ordered_quantity <= stock_before` | ordered_quantity, stock_before |
| INV-03 | ORDER | state_predicate | PENDING_CONFIRMATION, CONFIRMED, PAID, CANCELLED | `order_total == SUM_PRODUCT(order_items, 'quantity', 'unit_price')` | order_total, order_items[] |
| INV-04 | PAYMENT | state_predicate | PAID; khi `order_status == PAID` | `payment_count >= 1` | order_status, payment_count |
| INV-05 | ORDER | state_predicate | CANCELLED; khi `order_status == CANCELLED` | `stock_restoration_count == 1` | order_status, stock_restoration_count |
| INV-06 | ORDER | model_structure | (cấu trúc mô hình) | `no_event_from_state` | - (chỉ cần mô hình) |
| INV-07 | ORDER | model_structure | (cấu trúc mô hình) | `event_only_from_states` | - (chỉ cần mô hình) |
| INV-08 | PAYMENT | model_structure | (cấu trúc mô hình) | `event_only_from_states` | - (chỉ cần mô hình) |
| INV-09 | PAYMENT | state_predicate | mọi state | `payment_count <= 1` | payment_count |
| INV-10 | ORDER | model_structure | (cấu trúc mô hình) | `no_event_from_state` | - (chỉ cần mô hình) |
| INV-11 | GLOBAL | state_predicate | mọi state | `stock_after >= 0` | stock_after |
| INV-12 | PAYMENT | state_predicate | PAID; khi `order_status == PAID` | `payment_amount == order_total` | order_status, payment_amount, order_total |

`INV-06/07/08/10` là thuộc tính của **sự kiện** (không phải vị từ trên một snapshot biến), nên được kiểm tra trên **cấu trúc mô hình**
(`kind: model_structure`); hành vi backend tương ứng được chứng minh bằng các negative NT05–NT11, NT20 trong test conformance.

## 5. Ma trận truy vết `security_rules.json` ↔ EFSM

Cột *Transition* = rule được thể hiện trong guard/action; *Negative* = ca kiểm thử âm tính; *Invariant* = bất biến liên quan.
Mọi rule `IMPLEMENTED` đều có ít nhất một liên kết (được test `test_every_implemented_rule_is_modelled` bảo vệ).

| Rule | Tên | Status | Transition (guard/action) | Negative | Invariant | Mutant |
|---|---|---|---|---|---|---|
| BSR-CART-01 | Positive Cart Quantity | IMPLEMENTED | T01, T02 | NT01, NT17 | INV-01 | BUG-01 |
| BSR-CART-02 | Cart Quantity Must Not Exceed Stock | IMPLEMENTED | T01, T02 | NT02, NT16 | - | BUG-02 |
| BSR-CART-03 | Cart Product Must Exist | IMPLEMENTED | T01, T02 | NT19 | - | BUG-02A |
| BSR-CART-04 | Update Only Existing Cart Item | IMPLEMENTED | T02 | NT15 | - | - |
| BSR-CART-05 | Delete Only Existing Cart Item | IMPLEMENTED | T03 | NT18 | - | - |
| BSR-ORDER-01 | Order Requires Non-empty Cart | IMPLEMENTED | T04 | NT03 | - | BUG-03 |
| BSR-ORDER-02 | Allowed Payment Method | IMPLEMENTED | T04 | NT13 | - | - |
| BSR-ORDER-03 | Valid Customer Order Fields | IMPLEMENTED | T04 | NT14 | - | - |
| BSR-ORDER-04 | Order Initial State | IMPLEMENTED | T04 | - | - | - |
| BSR-ORDER-05 | Confirm Only Pending Confirmation Order | IMPLEMENTED | T05 | NT05, NT10, NT20 | INV-07 | BUG-05 |
| BSR-ORDER-06 | Order Creation Uses Server-side Product Price | IMPLEMENTED | T04 | - | INV-03 | BUG-10 |
| BSR-STOCK-01 | Stock Must Cover Order Quantity | IMPLEMENTED | T04 | NT04 | INV-02 | BUG-04 |
| BSR-STOCK-02 | Order Creation Deducts Stock Exactly | IMPLEMENTED | T04 | - | - | - |
| BSR-STOCK-03 | Stock Must Remain Non-negative | IMPLEMENTED | T04 | NT04 | INV-02, INV-11 | BUG-04 |
| BSR-STOCK-04 | Cancellation Restores Stock | IMPLEMENTED | T08, T09 | - | INV-05 | - |
| BSR-STOCK-05 | Stock Restoration Must Not Be Duplicated | IMPLEMENTED | T08, T09 | NT09 | INV-05, INV-10 | BUG-09 |
| BSR-CANCEL-01 | Cancellation Allowed Only in Defined States | IMPLEMENTED | T08, T09 | NT08, NT09 | INV-06, INV-10 | BUG-08 |
| BSR-CANCEL-02 | Paid Order Cancellation Prevention | IMPLEMENTED | T08, T09 | NT08 | INV-06 | BUG-08 |
| BSR-CANCEL-03 | Cancellation State Update | IMPLEMENTED | T08, T09 | - | - | - |
| BSR-PAYMENT-01 | Payment Only for Confirmed Order | IMPLEMENTED | T06 | NT06 | INV-08 | BUG-06 |
| BSR-PAYMENT-02 | Successful Payment Sets Order Paid | IMPLEMENTED | T06 | - | INV-04 | - |
| BSR-PAYMENT-03 | Failed Payment Does Not Set Order Paid | IMPLEMENTED | T07 | - | - | - |
| BSR-PAYMENT-04 | Prevent Payment for Cancelled or Non-confirmed Order | IMPLEMENTED | T06, T07 | NT06, NT11 | INV-08 | BUG-06 |
| BSR-PAYMENT-05 | Prevent Re-payment of Paid Order | IMPLEMENTED | - | NT07 | INV-08, INV-09 | BUG-07 |
| BSR-PAYMENT-06 | Payment Amount Derived from Order Total | IMPLEMENTED | T06 | - | INV-12 | BUG-10 |
| BSR-PAYMENT-07 | Payment Simulation Result Must Be Valid | IMPLEMENTED | T06, T07 | NT12 | - | - |
| BSR-AUTH-01 | Order Ownership | NOT_APPLICABLE | - | - | - | - |
| BSR-CANCEL-COMPLETED | Prevent Cancellation of Completed Order | NOT_APPLICABLE | - | - | - | - |
| BSR-PAYMENT-EXTERNAL | External Payment Gateway Verification | NOT_APPLICABLE | - | - | - | - |

Các rule `NOT_APPLICABLE` (ownership/authentication, trạng thái `COMPLETED`, cổng thanh toán thật) **không** được coi là đã kiểm thử,
vì website chưa có tương ứng; chúng được khai báo trong `traceability.excluded_rules` của mô hình.
`security_rules.json` **không bị thay đổi**: mapping đặt ở phía mô hình (`rule_refs`, `security_event`) và được test đối chiếu tự động.

## 6. Sai lệch đã phát hiện

| # | Phát hiện | Bằng chứng | Phân loại | Xử lý |
|---|---|---|---|---|
| D1 | NT12 ghi HTTP 422, backend trả **400** | `payment.py:25-26` (HTTPException 400, schema `simulate_result: str` không ràng buộc); rule BSR-PAYMENT-07 cũng ghi 400; chạy thật = 400 | MODEL_ERROR | sửa NT12 → 400, rule ref → PAYMENT-07 |
| D2 | T02 không có `cart_item_exists`/`product_exists` trong guard, dù backend trả 404 | `cart.py:87-93`; BSR-CART-03/04 | MODEL_ERROR | thêm 2 điều kiện; thêm NT15, NT19 |
| D3 | T01 gán biến `cart_quantity` không được khai báo; mọi action là chuỗi mô tả nên không mô phỏng được | model v1 | REPRESENTATION_GAP | action có cấu trúc (`assign`/`effect`); cập nhật `current_qty`, `cart_item_exists`, `cart_items_count` |
| D4 | `rule_refs` sai: T03→CART-04 (đúng CART-05); T05/NT05/NT10→ORDER-04 (đúng ORDER-05); NT08→CANCEL-03 (đúng CANCEL-01/02); NT12→PAYMENT-03 (đúng PAYMENT-07); T02 thiếu CART-02/04; T04 thiếu ORDER-04/06; T06/T07 thiếu PAYMENT-07 | đối chiếu `security_rules.json` | MODEL_ERROR | sửa toàn bộ; thêm test đối chiếu `security_event` ↔ `event` của rule |
| D5 | T04 không biểu diễn ràng buộc Pydantic của tên/điện thoại/địa chỉ (BSR-ORDER-03 đã được T04 trích dẫn) | `schemas.py:14-18` | REPRESENTATION_GAP | thêm biến `*_length` + điều kiện guard; thêm NT13, NT14 |
| D6 | Endpoint `DELETE /api/cart` chưa có trong mô hình | `routes/cart.py:31-34` | REPRESENTATION_GAP | thêm **T10** (không có BSR tương ứng) |
| D7 | Thiếu negative cho: update item không có, update vượt kho, update ≤0, xóa item không có, product không tồn tại, confirm đơn PAID | code + BSR-CART-01..05, ORDER-05 | REPRESENTATION_GAP | thêm NT15–NT20 (+NT13/NT14) |
| D8 | Invariant v1 là văn bản tự do (SUM, `exists payment`, `=> cancel rejected`...) nên evaluator v1 không đánh giá thật | INV-03/04/06..10 | REPRESENTATION_GAP | chuyển thành `check` có cấu trúc, `model_structure`, hoặc biến dẫn xuất (`order_items`, `payment_count`); thêm INV-11/12 |
| D9 | `ordered_quantity`, `stock` mô tả một dòng hàng; backend kiểm tra mọi dòng | `order.py:49-54` | REPRESENTATION_GAP | ghi vào `abstractions`; INV-03 vẫn là tổng (SUM_PRODUCT) |
| D10 | NT04 (`ordered_quantity > stock`) không thể đạt chỉ bằng API: giỏ luôn ≤ stock khi thêm, tạo đơn xóa giỏ, hủy chỉ tăng kho | `cart.py`, `order.py` | REPRESENTATION_GAP | đánh dấu `api_reachable: false`; cần seed DB hoặc mutant BUG-04 |
| D11 | 404 do `order_id` không tồn tại là tra cứu tài nguyên, không đổi state | `order.py:122` | REPRESENTATION_GAP | không mô hình hóa; ghi trong `abstractions` |
| D12 | `pay_order` **không dùng** `order.payment_method`: đơn `COD` vẫn thanh toán được bằng mô phỏng; số lần thanh toán thất bại không giới hạn | `payment.py` | REQUIREMENT_AMBIGUITY | không sửa backend; cần chủ đề tài xác nhận chính sách |
| D13 | `workflow.json` rỗng (0 byte) | file trong zip | REPRESENTATION_GAP | không dùng; chỉ ghi nhận |

**Không phát hiện `IMPLEMENTATION_DEFECT` có đủ bằng chứng**, nên không sửa backend.
