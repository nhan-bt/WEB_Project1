"""Payment simulation business rules backed by SQLite."""
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import SessionLocal
from app.models import Order, Payment
from app.service.order import _order_to_dict


def pay_order(order_id: int, simulate_result: str):
    payment_id = None
    with SessionLocal() as db:
        with db.begin():
            order = db.get(Order, order_id)
            if order is None:
                raise HTTPException(status_code=404, detail="Order not found")
            if order.status != "CONFIRMED":
                raise HTTPException(
                    status_code=409,
                    detail="Chi duoc thanh toan don hang da xac nhan",
                )
            if simulate_result not in {"SUCCESS", "FAILED"}:
                raise HTTPException(status_code=400, detail="Ket qua thanh toan khong hop le")

            payment = Payment(
                order_id=order_id,
                amount=order.total_amount,
                result=simulate_result,
                created_at=datetime.now(timezone.utc),
            )
            db.add(payment)
            db.flush()
            payment_id = payment.id

            if simulate_result == "SUCCESS":
                order.status = "PAID"

    # Read committed values after the transaction has completed.
    with SessionLocal() as db:
        payment = db.get(Payment, payment_id)
        order = db.scalar(
            select(Order).options(selectinload(Order.items)).where(Order.id == order_id)
        )
        payment_data = {
            "id": payment.id,
            "order_id": payment.order_id,
            "amount": payment.amount,
            "result": payment.result,
        }
        order_data = _order_to_dict(order)

    message = (
        "Thanh toan thanh cong"
        if simulate_result == "SUCCESS"
        else "Thanh toan that bai, ban co the thu lai"
    )
    return {"message": message, "payment": payment_data, "order": order_data}


def list_payments():
    with SessionLocal() as db:
        payments = db.scalars(select(Payment).order_by(Payment.id)).all()
        return [
            {
                "id": payment.id,
                "order_id": payment.order_id,
                "amount": payment.amount,
                "result": payment.result,
            }
            for payment in payments
        ]
