from fastapi import APIRouter
from app.schemas import PaymentInput
from app.service import payment as payment_service

router = APIRouter(tags=["payments"])


@router.post("/api/orders/{order_id}/pay")
def pay_order(order_id: int, data: PaymentInput):
    return payment_service.pay_order(order_id, data.simulate_result)


@router.get("/api/payments")
def get_payments():
    return payment_service.list_payments()
