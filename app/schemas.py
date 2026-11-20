"""Pydantic schemas used to validate API input."""
from pydantic import BaseModel, Field


class CartItemInput(BaseModel):
    product_id: int = Field(gt=0)
    quantity: int = Field(gt=0)


class QuantityInput(BaseModel):
    quantity: int = Field(gt=0)


class OrderInput(BaseModel):
    customer_name: str = Field(min_length=2, max_length=100)
    phone: str = Field(min_length=9, max_length=15)
    address: str = Field(min_length=5, max_length=255)
    payment_method: str = "MOCK"


class PaymentInput(BaseModel):
    simulate_result: str = "SUCCESS"
