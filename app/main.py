"""Application entry point: configure FastAPI, routes, and static storefront."""
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.database import init_db
from app.routes import cart, orders, payments, products

app = FastAPI(title="Online Shop API", version="0.3.0")

# Keep the existing SQLite initialization and API routes unchanged.
init_db()

app.include_router(products.router)
app.include_router(cart.router)
app.include_router(orders.router)
app.include_router(payments.router)

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def home():
    """Serve the storefront homepage; API docs remain available at /docs."""
    return FileResponse(STATIC_DIR / "index.html")
