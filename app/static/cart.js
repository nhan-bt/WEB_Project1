const money = (value) => new Intl.NumberFormat("vi-VN", {
  style: "currency", currency: "VND", maximumFractionDigits: 0
}).format(Number(value) || 0);

const cartContent = document.getElementById("cart-content");
const cartStatus = document.getElementById("cart-status");
const cartCount = document.getElementById("cart-count");
const toast = document.getElementById("toast");
let toastTimer;
let busyProducts = new Set();

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[char]));
}

function visualFor(name) {
  const value = String(name || "").toLocaleLowerCase("vi-VN");
  if (value.includes("áo") || value.includes("ao") || value.includes("thun")) return "👕";
  if (value.includes("quần") || value.includes("quan") || value.includes("jean")) return "👖";
  if (value.includes("giày") || value.includes("giay") || value.includes("thể thao")) return "👟";
  return "🛍️";
}

async function api(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) }
  });
  let data = {};
  try { data = await response.json(); } catch { /* empty response */ }
  if (!response.ok) throw new Error(data.detail || data.message || `Yêu cầu thất bại (${response.status})`);
  return data;
}

function showToast(message, isError = false) {
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 2800);
}

function renderCart(cart) {
  const items = Array.isArray(cart.items) ? cart.items : [];
  cartCount.textContent = Number(cart.total_quantity) || 0;
  cartStatus.textContent = items.length ? `${items.length} loại sản phẩm trong giỏ hàng` : "Giỏ hàng hiện đang trống";

  if (!items.length) {
    cartContent.innerHTML = `
      <section class="cart-empty-page">
        <div class="cart-empty-illustration">🛒</div>
        <h2>Chưa có sản phẩm nào</h2>
        <p>Hãy quay lại cửa hàng và chọn những sản phẩm bạn yêu thích.</p>
        <a class="primary-button" href="/">Khám phá sản phẩm <span>→</span></a>
      </section>`;
    return;
  }

  cartContent.innerHTML = `
    <section class="cart-items-panel">
      <div class="cart-items-heading"><h2>Sản phẩm đã chọn</h2><span>${Number(cart.total_quantity) || 0} sản phẩm</span></div>
      <div class="cart-items-list">
        ${items.map((item) => {
          const id = Number(item.product_id);
          const qty = Number(item.quantity) || 0;
          const stock = Number(item.stock) || 0;
          const locked = busyProducts.has(id);
          return `
            <article class="cart-line ${locked ? "is-busy" : ""}">
              <div class="cart-line-visual">${visualFor(item.name)}</div>
              <div class="cart-line-main">
                <span class="product-category">BLUEMART</span>
                <h3>${escapeHtml(item.name)}</h3>
                <p class="cart-line-unit">${money(item.price)} / sản phẩm</p>
                <div class="quantity-control" aria-label="Điều chỉnh số lượng">
                  <button type="button" data-qty="-1" data-product-id="${id}" aria-label="Giảm số lượng" ${locked || qty <= 1 ? "disabled" : ""}>−</button>
                  <output>${qty}</output>
                  <button type="button" data-qty="1" data-product-id="${id}" aria-label="Tăng số lượng" ${locked || qty >= stock ? "disabled" : ""}>+</button>
                  <span class="quantity-stock">Tồn kho: ${stock}</span>
                </div>
              </div>
              <div class="cart-line-end">
                <strong>${money(item.subtotal)}</strong>
                <button class="remove-item" type="button" data-remove-product="${id}" ${locked ? "disabled" : ""}>Xóa</button>
              </div>
            </article>`;
        }).join("")}
      </div>
      <div class="cart-items-bottom"><button type="button" class="text-button" id="clear-cart">Xóa toàn bộ giỏ hàng</button><a class="text-button" href="/">← Tiếp tục mua sắm</a></div>
    </section>
    <aside class="order-summary">
      <span class="section-kicker">TÓM TẮT ĐƠN HÀNG</span>
      <h2>Thông tin thanh toán</h2>
      <div class="summary-row"><span>Số lượng sản phẩm</span><strong>${Number(cart.total_quantity) || 0}</strong></div>
      <div class="summary-row"><span>Tạm tính</span><strong>${money(cart.total_amount)}</strong></div>
      <div class="summary-row"><span>Phí vận chuyển</span><strong class="free-shipping">Tính sau</strong></div>
      <div class="summary-total"><span>Tổng tiền</span><strong>${money(cart.total_amount)}</strong></div>
      <a class="primary-button checkout-button" href="/static/checkout.html">Tiến hành đặt hàng <span>→</span></a>
      <p class="summary-note">Tồn kho sẽ được kiểm tra lại khi tạo đơn hàng.</p>
      <div class="secure-note"><span>✓</span><span><strong>Thanh toán mô phỏng an toàn</strong><small>Đây là website demo phục vụ học tập.</small></span></div>
    </aside>`;
}

async function loadCart() {
  cartStatus.textContent = "Đang tải giỏ hàng...";
  try {
    const cart = await api("/api/cart");
    renderCart(cart);
  } catch (error) {
    cartStatus.textContent = "Không thể tải giỏ hàng.";
    cartContent.innerHTML = `<section class="cart-empty-page"><h2>Có lỗi xảy ra</h2><p>${escapeHtml(error.message)}. Hãy kiểm tra máy chủ FastAPI.</p><a class="primary-button" href="/">Quay lại trang chủ</a></section>`;
  }
}

async function changeQuantity(productId, delta) {
  if (busyProducts.has(productId)) return;
  busyProducts.add(productId);
  await loadCart();
  try {
    const currentCart = await api("/api/cart");
    const item = currentCart.items.find((row) => Number(row.product_id) === productId);
    if (!item) return;
    const quantity = Number(item.quantity) + delta;
    if (quantity < 1) return;
    await api(`/api/cart/items/${productId}`, {
      method: "PATCH", body: JSON.stringify({ quantity })
    });
    showToast("Đã cập nhật số lượng sản phẩm.");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    busyProducts.delete(productId);
    await loadCart();
  }
}

async function removeItem(productId) {
  if (busyProducts.has(productId)) return;
  busyProducts.add(productId);
  await loadCart();
  try {
    await api(`/api/cart/items/${productId}`, { method: "DELETE" });
    showToast("Đã xóa sản phẩm khỏi giỏ hàng.");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    busyProducts.delete(productId);
    await loadCart();
  }
}

async function clearCart() {
  if (!window.confirm("Bạn có chắc muốn xóa tất cả sản phẩm trong giỏ hàng?")) return;
  try {
    await api("/api/cart", { method: "DELETE" });
    showToast("Đã xóa toàn bộ giỏ hàng.");
    await loadCart();
  } catch (error) {
    showToast(error.message, true);
  }
}

cartContent.addEventListener("click", (event) => {
  const quantityButton = event.target.closest("[data-qty]");
  const removeButton = event.target.closest("[data-remove-product]");
  if (quantityButton) {
    changeQuantity(Number(quantityButton.dataset.productId), Number(quantityButton.dataset.qty));
  } else if (removeButton) {
    removeItem(Number(removeButton.dataset.removeProduct));
  } else if (event.target.closest("#clear-cart")) {
    clearCart();
  }
});

document.getElementById("year").textContent = new Date().getFullYear();
loadCart();
