import os

# Keep automated tests in a separate database so test resets never erase the demo DB.
os.environ["ONLINE_SHOP_DATABASE_URL"] = "sqlite:///./test_online_shop.db"

from fastapi.testclient import TestClient

from app.main import app
from app.database import reset_database

client = TestClient(app)


def setup_function():
    reset_database()


def create_order_from_cart(quantity=2):
    add = client.post("/api/cart/items", json={"product_id": 1, "quantity": quantity})
    assert add.status_code == 200
    response = client.post("/api/orders", json={
        "customer_name": "Nguyen Van A",
        "phone": "0900000000",
        "address": "Ha Noi, Viet Nam",
        "payment_method": "MOCK",
    })
    assert response.status_code == 201
    return response.json()["order"]


def test_products_and_cart():
    assert client.get("/api/products").status_code == 200
    assert client.get("/api/cart").json()["total_amount"] == 0
    response = client.post("/api/cart/items", json={"product_id": 1, "quantity": 2})
    assert response.status_code == 200
    assert response.json()["cart"]["total_amount"] == 300000


def test_reject_quantity_over_stock():
    response = client.post("/api/cart/items", json={"product_id": 1, "quantity": 11})
    assert response.status_code == 400


def test_order_confirm_and_pay_once():
    order = create_order_from_cart()
    order_id = order["id"]
    assert order["status"] == "PENDING_CONFIRMATION"
    assert client.post(f"/api/orders/{order_id}/confirm").status_code == 200
    assert client.post(f"/api/orders/{order_id}/pay", json={"simulate_result": "SUCCESS"}).status_code == 200
    assert client.get(f"/api/orders/{order_id}").json()["status"] == "PAID"
    assert client.post(f"/api/orders/{order_id}/pay", json={"simulate_result": "SUCCESS"}).status_code == 409


def test_cancel_order_restores_stock_and_blocks_payment():
    order = create_order_from_cart()
    order_id = order["id"]
    assert client.post(f"/api/orders/{order_id}/cancel").status_code == 200
    assert client.get(f"/api/orders/{order_id}").json()["status"] == "CANCELLED"
    assert client.get("/api/products/1").json()["stock"] == 10
    assert client.post(f"/api/orders/{order_id}/pay", json={"simulate_result": "SUCCESS"}).status_code == 409


def test_order_and_payment_data_are_persisted_in_sqlite():
    order = create_order_from_cart()
    order_id = order["id"]
    assert client.post(f"/api/orders/{order_id}/confirm").status_code == 200
    assert client.post(
        f"/api/orders/{order_id}/pay", json={"simulate_result": "SUCCESS"}
    ).status_code == 200

    # A fresh API request reads the saved order and payment from SQLite.
    assert client.get(f"/api/orders/{order_id}").json()["status"] == "PAID"
    payments = client.get("/api/payments").json()
    assert len(payments) == 1
    assert payments[0]["order_id"] == order_id
