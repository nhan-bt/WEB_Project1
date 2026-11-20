"""Kiểm chứng efsm_model.json với backend FastAPI THẬT (integration test).

Mỗi ca chạy API trên database test riêng (test_online_shop.db) và so HTTP status +
state thu được với kỳ vọng ĐỌC TỪ MÔ HÌNH (expected_http_status / expected.http_status).
Nếu mô hình sai, test này thất bại - không sửa kỳ vọng để test đạt.
Khác với tests/test_guard_evaluator.py (unit test, không dùng API).
"""
import json
import os
from pathlib import Path

# Giữ database test tách biệt với online_shop.db (phải đặt trước khi import app).
os.environ["ONLINE_SHOP_DATABASE_URL"] = "sqlite:///./test_online_shop.db"

import pytest
from fastapi.testclient import TestClient

from app.database import reset_database
from app.main import app
from guard_evaluator import GuardEvaluator

MODEL_PATH = Path(__file__).resolve().parents[1] / "efsm_model.json"
MODEL = json.loads(MODEL_PATH.read_text(encoding="utf-8"))
TRANSITIONS = {t["id"]: t for t in MODEL["transitions"]}
NEGATIVES = {n["id"]: n for n in MODEL["negative_transitions"]}
client = TestClient(app)

CUSTOMER = {"customer_name": "Nguyen Van A", "phone": "0900000000", "address": "Ha Noi, Viet Nam", "payment_method": "MOCK"}


def setup_function():
    reset_database()


# ---- helpers dựng state ----------------------------------------------------
def add(product_id=1, quantity=2):
    return client.post("/api/cart/items", json={"product_id": product_id, "quantity": quantity})


def new_order(quantity=2):
    assert add(1, quantity).status_code == 200
    response = client.post("/api/orders", json=CUSTOMER)
    assert response.status_code == 201
    return response.json()["order"]["id"]


def confirmed(quantity=2):
    oid = new_order(quantity)
    assert client.post(f"/api/orders/{oid}/confirm").status_code == 200
    return oid


def paid():
    oid = confirmed()
    assert client.post(f"/api/orders/{oid}/pay", json={"simulate_result": "SUCCESS"}).status_code == 200
    return oid


def cancelled():
    oid = new_order()
    assert client.post(f"/api/orders/{oid}/cancel").status_code == 200
    return oid


def status_of(oid):
    return client.get(f"/api/orders/{oid}").json()["status"]


def stock(pid=1):
    return client.get(f"/api/products/{pid}").json()["stock"]


# ---- transition hợp lệ: (id, chuẩn bị, hành động, state sau) ---------------------------
def _t01():
    return None, lambda _: add(1, 3), lambda _: (client.get("/api/cart").json()["items"][0]["quantity"] == 3)


def _t02():
    add(1, 2)
    return None, lambda _: client.patch("/api/cart/items/1", json={"quantity": 5}), lambda _: client.get("/api/cart").json()["items"][0]["quantity"] == 5


def _t03():
    add(1, 2)
    return None, lambda _: client.delete("/api/cart/items/1"), lambda _: client.get("/api/cart").json()["items"] == []


def _t10():
    add(1, 2)
    return None, lambda _: client.delete("/api/cart"), lambda _: client.get("/api/cart").json()["items"] == []


def _t04():
    add(1, 3)
    return None, lambda _: client.post("/api/orders", json=CUSTOMER), lambda r: (
        r.json()["order"]["status"] == "PENDING_CONFIRMATION" and stock() == 7 and client.get("/api/cart").json()["items"] == []
        and r.json()["order"]["total_amount"] == 450000)


def _t05():
    oid = new_order()
    return oid, lambda o: client.post(f"/api/orders/{o}/confirm"), lambda _: status_of(oid) == "CONFIRMED"


def _t06():
    oid = confirmed()
    return oid, lambda o: client.post(f"/api/orders/{o}/pay", json={"simulate_result": "SUCCESS"}), lambda _: (
        status_of(oid) == "PAID" and client.get("/api/payments").json()[0]["amount"] == 300000)


def _t07():
    oid = confirmed()
    return oid, lambda o: client.post(f"/api/orders/{o}/pay", json={"simulate_result": "FAILED"}), lambda _: (
        status_of(oid) == "CONFIRMED" and client.get("/api/payments").json()[0]["result"] == "FAILED")


def _t08():
    oid = new_order()
    return oid, lambda o: client.post(f"/api/orders/{o}/cancel"), lambda _: status_of(oid) == "CANCELLED" and stock() == 10


def _t09():
    oid = confirmed()
    return oid, lambda o: client.post(f"/api/orders/{o}/cancel"), lambda _: status_of(oid) == "CANCELLED" and stock() == 10


@pytest.mark.parametrize("tid, builder", [("T01", _t01), ("T02", _t02), ("T03", _t03), ("T10", _t10), ("T04", _t04),
                                          ("T05", _t05), ("T06", _t06), ("T07", _t07), ("T08", _t08), ("T09", _t09)])
def test_valid_transition_matches_backend(tid, builder):
    arg, act, post_check = builder()
    response = act(arg)
    assert response.status_code == TRANSITIONS[tid]["expected_http_status"], response.text
    assert post_check(response), f"{tid}: state/data after the call does not match the model"


# ---- negative transition: (id, chuẩn bị, hành động) ----------------------------------------
NEGATIVE_CASES = {
    "NT01": lambda: add(1, 0),
    "NT02": lambda: (add(1, 8), add(1, 5))[1],
    "NT03": lambda: client.post("/api/orders", json=CUSTOMER),
    "NT05": lambda: client.post(f"/api/orders/{confirmed()}/confirm"),
    "NT06": lambda: client.post(f"/api/orders/{new_order()}/pay", json={"simulate_result": "SUCCESS"}),
    "NT07": lambda: client.post(f"/api/orders/{paid()}/pay", json={"simulate_result": "SUCCESS"}),
    "NT08": lambda: client.post(f"/api/orders/{paid()}/cancel"),
    "NT09": lambda: client.post(f"/api/orders/{cancelled()}/cancel"),
    "NT10": lambda: client.post(f"/api/orders/{cancelled()}/confirm"),
    "NT11": lambda: client.post(f"/api/orders/{cancelled()}/pay", json={"simulate_result": "SUCCESS"}),
    "NT12": lambda: client.post(f"/api/orders/{confirmed()}/pay", json={"simulate_result": "MAYBE"}),
    "NT13": lambda: (add(1, 1), client.post("/api/orders", json={**CUSTOMER, "payment_method": "PAYPAL"}))[1],
    "NT14": lambda: (add(1, 1), client.post("/api/orders", json={**CUSTOMER, "customer_name": "A"}))[1],
    "NT15": lambda: client.patch("/api/cart/items/1", json={"quantity": 2}),
    "NT16": lambda: (add(1, 1), client.patch("/api/cart/items/1", json={"quantity": 11}))[1],
    "NT17": lambda: (add(1, 1), client.patch("/api/cart/items/1", json={"quantity": 0}))[1],
    "NT18": lambda: client.delete("/api/cart/items/1"),
    "NT19": lambda: add(999, 1),
    "NT20": lambda: client.post(f"/api/orders/{paid()}/confirm"),
}


def test_every_reachable_negative_transition_has_a_conformance_case():
    reachable = {nid for nid, n in NEGATIVES.items() if n.get("api_reachable", True)}
    assert reachable == set(NEGATIVE_CASES)
    assert NEGATIVES["NT04"]["api_reachable"] is False


@pytest.mark.parametrize("nid", sorted(NEGATIVE_CASES))
def test_negative_transition_is_rejected_with_modelled_status(nid):
    response = NEGATIVE_CASES[nid]()
    assert response.status_code == NEGATIVES[nid]["expected"]["http_status"], response.text


def test_rejected_calls_leave_state_and_stock_unchanged():
    oid = paid()
    stock_before = stock()
    assert client.post(f"/api/orders/{oid}/cancel").status_code == 409
    assert client.post(f"/api/orders/{oid}/pay", json={"simulate_result": "SUCCESS"}).status_code == 409
    assert status_of(oid) == "PAID" and stock() == stock_before
    assert len(client.get("/api/payments").json()) == 1


def test_status_check_precedes_result_validation_on_pay():
    """Backend kiểm tra status trước simulate_result: đơn chưa xác nhận + kết quả sai -> 409, không phải 400."""
    response = client.post(f"/api/orders/{new_order()}/pay", json={"simulate_result": "MAYBE"})
    assert response.status_code == 409


def test_model_status_codes_agree_with_evaluator_on_real_data():
    """Dữ liệu thật từ backend đưa vào evaluator: T06 được phép đúng khi backend trả 200."""
    evaluator = GuardEvaluator(MODEL_PATH)
    oid = confirmed()
    order = client.get(f"/api/orders/{oid}").json()
    variables = {"order_status": order["status"], "payment_result": "SUCCESS"}
    assert evaluator.check_transition("T06", "CONFIRMED", variables).enabled
    assert client.post(f"/api/orders/{oid}/pay", json={"simulate_result": "SUCCESS"}).status_code == 200
    order = client.get(f"/api/orders/{oid}").json()
    assert order["status"] == "PAID"
    assert evaluator.check_transition("T06", "PAID", {"order_status": "PAID", "payment_result": "SUCCESS"}).status == "STATE_MISMATCH"
