"""Product and cart business rules backed by SQLite."""
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.database import SessionLocal
from app.models import CartItem, Product


def _product_to_dict(product: Product) -> dict:
    return {
        "id": product.id,
        "name": product.name,
        "price": product.price,
        "stock": product.stock,
    }


def list_products():
    with SessionLocal() as db:
        products = db.scalars(select(Product).order_by(Product.id)).all()
        return [_product_to_dict(product) for product in products]


def get_product(product_id: int):
    with SessionLocal() as db:
        product = db.get(Product, product_id)
        if product is None:
            raise HTTPException(status_code=404, detail="Product not found")
        return _product_to_dict(product)


def get_cart_data():
    with SessionLocal() as db:
        cart_items = db.scalars(
            select(CartItem).options(joinedload(CartItem.product)).order_by(CartItem.product_id)
        ).all()

        items = []
        total_amount = 0
        total_quantity = 0
        for cart_item in cart_items:
            product = cart_item.product
            subtotal = product.price * cart_item.quantity
            items.append({
                "product_id": product.id,
                "name": product.name,
                "price": product.price,
                "quantity": cart_item.quantity,
                "stock": product.stock,
                "subtotal": subtotal,
            })
            total_amount += subtotal
            total_quantity += cart_item.quantity

        return {
            "items": items,
            "total_quantity": total_quantity,
            "total_amount": total_amount,
        }


def add_cart_item(product_id: int, quantity: int):
    with SessionLocal() as db:
        with db.begin():
            product = db.get(Product, product_id)
            if product is None:
                raise HTTPException(status_code=404, detail="Product not found")

            item = db.get(CartItem, product_id)
            current_quantity = item.quantity if item else 0
            new_quantity = current_quantity + quantity
            if new_quantity > product.stock:
                raise HTTPException(status_code=400, detail="So luong vuot qua ton kho")

            if item is None:
                db.add(CartItem(product_id=product_id, quantity=new_quantity))
            else:
                item.quantity = new_quantity

    return get_cart_data()


def update_cart_item(product_id: int, quantity: int):
    with SessionLocal() as db:
        with db.begin():
            product = db.get(Product, product_id)
            if product is None:
                raise HTTPException(status_code=404, detail="Product not found")

            item = db.get(CartItem, product_id)
            if item is None:
                raise HTTPException(status_code=404, detail="San pham khong co trong gio hang")
            if quantity > product.stock:
                raise HTTPException(status_code=400, detail="So luong vuot qua ton kho")
            item.quantity = quantity

    return get_cart_data()


def delete_cart_item(product_id: int):
    with SessionLocal() as db:
        with db.begin():
            item = db.get(CartItem, product_id)
            if item is None:
                raise HTTPException(status_code=404, detail="San pham khong co trong gio hang")
            db.delete(item)

    return get_cart_data()


def clear_cart():
    with SessionLocal() as db:
        with db.begin():
            for item in db.scalars(select(CartItem)).all():
                db.delete(item)
    return get_cart_data()
