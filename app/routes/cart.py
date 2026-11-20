from fastapi import APIRouter
from app.schemas import CartItemInput, QuantityInput
from app.service import cart as cart_service

router = APIRouter(tags=["cart"])


@router.get("/api/cart")
def get_cart():
    return cart_service.get_cart_data()


@router.post("/api/cart/items")
def add_cart_item(data: CartItemInput):
    cart = cart_service.add_cart_item(data.product_id, data.quantity)
    return {"message": "Da them san pham vao gio hang", "cart": cart}


@router.patch("/api/cart/items/{product_id}")
def update_cart_item(product_id: int, data: QuantityInput):
    cart = cart_service.update_cart_item(product_id, data.quantity)
    return {"message": "Da cap nhat so luong", "cart": cart}


@router.delete("/api/cart/items/{product_id}")
def delete_cart_item(product_id: int):
    cart = cart_service.delete_cart_item(product_id)
    return {"message": "Da xoa san pham", "cart": cart}


@router.delete("/api/cart")
def clear_cart():
    cart = cart_service.clear_cart()
    return {"message": "Da lam trong gio hang", "cart": cart}
