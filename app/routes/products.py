from fastapi import APIRouter
from app.service import cart as cart_service

router = APIRouter(tags=["products"])


@router.get("/api/products")
def get_products():
    return cart_service.list_products()


@router.get("/api/products/{product_id}")
def get_product(product_id: int):
    return cart_service.get_product(product_id)
