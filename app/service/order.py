"""Order business rules and state transitions backed by SQLite."""
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import SessionLocal
from app.models import CartItem, Order, OrderItem, Product

ALLOWED_PAYMENT_METHODS = {"MOCK", "COD"}


def _order_to_dict(order: Order) -> dict:
    return {
        "id": order.id,
        "customer_name": order.customer_name,
        "phone": order.phone,
        "address": order.address,
        "payment_method": order.payment_method,
        "items": [
            {
                "product_id": item.product_id,
                "product_name": item.product_name,
                "unit_price": item.unit_price,
                "quantity": item.quantity,
                "subtotal": item.subtotal,
            }
            for item in order.items
        ],
        "total_amount": order.total_amount,
        "status": order.status,
        "created_at": order.created_at.isoformat() if order.created_at else None,
    }


def create_order(data):
    created_order_id = None
    with SessionLocal() as db:
        with db.begin():
            cart_items = db.scalars(select(CartItem).order_by(CartItem.product_id)).all()
            if not cart_items:
                raise HTTPException(status_code=400, detail="Gio hang dang trong")
            if data.payment_method not in ALLOWED_PAYMENT_METHODS:
                raise HTTPException(status_code=400, detail="Phuong thuc thanh toan khong hop le")

            prepared_items = []
            total_amount = 0
            for cart_item in cart_items:
                product = db.get(Product, cart_item.product_id)
                if product is None:
                    raise HTTPException(status_code=404, detail="Product not found")
                if cart_item.quantity <= 0 or cart_item.quantity > product.stock:
                    raise HTTPException(status_code=400, detail="So luong san pham khong hop le")

                subtotal = product.price * cart_item.quantity
                prepared_items.append({
                    "product": product,
                    "product_id": product.id,
                    "product_name": product.name,
                    "unit_price": product.price,
                    "quantity": cart_item.quantity,
                    "subtotal": subtotal,
                })
                total_amount += subtotal

            order = Order(
                customer_name=data.customer_name,
                phone=data.phone,
                address=data.address,
                payment_method=data.payment_method,
                total_amount=total_amount,
                status="PENDING_CONFIRMATION",
                created_at=datetime.now(timezone.utc),
            )
            db.add(order)
            db.flush()
            created_order_id = order.id

            for item in prepared_items:
                item["product"].stock -= item["quantity"]
                db.add(OrderItem(
                    order_id=order.id,
                    product_id=item["product_id"],
                    product_name=item["product_name"],
                    unit_price=item["unit_price"],
                    quantity=item["quantity"],
                    subtotal=item["subtotal"],
                ))

            for cart_item in cart_items:
                db.delete(cart_item)

    return get_order(created_order_id)


def list_orders():
    with SessionLocal() as db:
        orders = db.scalars(
            select(Order).options(selectinload(Order.items)).order_by(Order.id)
        ).all()
        return [_order_to_dict(order) for order in orders]


def get_order(order_id: int):
    with SessionLocal() as db:
        order = db.scalar(
            select(Order)
            .options(selectinload(Order.items))
            .where(Order.id == order_id)
        )
        if order is None:
            raise HTTPException(status_code=404, detail="Order not found")
        return _order_to_dict(order)


def confirm_order(order_id: int):
    with SessionLocal() as db:
        with db.begin():
            order = db.get(Order, order_id)
            if order is None:
                raise HTTPException(status_code=404, detail="Order not found")
            if order.status != "PENDING_CONFIRMATION":
                raise HTTPException(
                    status_code=409,
                    detail="Don hang khong the xac nhan o trang thai hien tai",
                )
            order.status = "CONFIRMED"
    return get_order(order_id)


def cancel_order(order_id: int):
    with SessionLocal() as db:
        with db.begin():
            order = db.scalar(
                select(Order).options(selectinload(Order.items)).where(Order.id == order_id)
            )
            if order is None:
                raise HTTPException(status_code=404, detail="Order not found")
            if order.status not in {"PENDING_CONFIRMATION", "CONFIRMED"}:
                raise HTTPException(
                    status_code=409,
                    detail="Don hang khong the huy o trang thai hien tai",
                )

            # Restore stock and change status in one database transaction.
            for item in order.items:
                if item.product_id is not None:
                    product = db.get(Product, item.product_id)
                    if product is not None:
                        product.stock += item.quantity
            order.status = "CANCELLED"

    return get_order(order_id)
