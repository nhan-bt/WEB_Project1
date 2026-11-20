from fastapi import APIRouter, status
from app.schemas import OrderInput
from app.service import order as order_service

router = APIRouter(tags=["orders"])


@router.post("/api/orders", status_code=status.HTTP_201_CREATED)
def create_order(data: OrderInput):
    order = order_service.create_order(data)
    return {"message": "Dat hang thanh cong", "order": order}


@router.get("/api/orders")
def get_orders():
    return order_service.list_orders()


@router.get("/api/orders/{order_id}")
def get_order(order_id: int):
    return order_service.get_order(order_id)


@router.post("/api/orders/{order_id}/confirm")
def confirm_order(order_id: int):
    order = order_service.confirm_order(order_id)
    return {"message": "Xac nhan don hang thanh cong", "order": order}


@router.post("/api/orders/{order_id}/cancel")
def cancel_order(order_id: int):
    order = order_service.cancel_order(order_id)
    return {"message": "Huy don hang thanh cong", "order": order}
