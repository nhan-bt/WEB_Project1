"""Temporary in-memory data store.

This is intentionally temporary for the refactoring stage. The next stage can
replace this module with SQLite persistence without moving business rules back
into the route handlers.
"""
from typing import Any

products: list[dict[str, Any]] = [
    {"id": 1, "name": "Ao thun", "price": 150000, "stock": 10},
    {"id": 2, "name": "Quan jean", "price": 350000, "stock": 5},
    {"id": 3, "name": "Giay the thao", "price": 600000, "stock": 3},
]

# product_id -> quantity
cart: dict[int, int] = {}
orders: list[dict[str, Any]] = []
payments: list[dict[str, Any]] = []
next_order_id = 1


def reset_store() -> None:
    """Reset data to initial values; useful for isolated automated tests."""
    global products, next_order_id
    products = [
        {"id": 1, "name": "Ao thun", "price": 150000, "stock": 10},
        {"id": 2, "name": "Quan jean", "price": 350000, "stock": 5},
        {"id": 3, "name": "Giay the thao", "price": 600000, "stock": 3},
    ]
    cart.clear()
    orders.clear()
    payments.clear()
    next_order_id = 1
