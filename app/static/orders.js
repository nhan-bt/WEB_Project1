const orderList = document.getElementById("orders-list");
const statusLine = document.getElementById("orders-status");
const money = (value) => new Intl.NumberFormat("vi-VN", {
  style: "currency", currency: "VND", maximumFractionDigits: 0
}).format(Number(value) || 0);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
}[char]));
const statusInfo = {
  PENDING_CONFIRMATION: { label: "Chờ xác nhận", className: "pending" },
  CONFIRMED: { label: "Đã xác nhận · Chờ thanh toán", className: "confirmed" },
  PAID: { label: "Đã thanh toán", className: "paid" },
  CANCELLED: { label: "Đã hủy", className: "cancelled" }
};

async function api(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) }
  });
  let data = {};
  try { data = await response.json(); } catch { /* response may be empty */ }
  if (!response.ok) throw new Error(data.detail || data.message || `Yêu cầu thất bại (${response.status})`);
  return data;
}

function formatDate(value) {
  if (!value) return "Không rõ thời gian";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? escapeHtml(value) : date.toLocaleString("vi-VN");
}

function renderOrders(orders) {
  if (!Array.isArray(orders) || orders.length === 0) {
    orderList.innerHTML = `<section class="cart-empty-page"><div class="cart-empty-illustration">📦</div><h2>Bạn chưa có đơn hàng</h2><p>Đơn hàng sẽ xuất hiện tại đây sau khi bạn đặt hàng thành công.</p><a class="primary-button" href="/">Bắt đầu mua sắm</a></section>`;
    return;
  }

  orderList.innerHTML = [...orders].reverse().map((order) => {
    const id = Number(order.id);
    const status = statusInfo[order.status] || { label: order.status || "Không xác định", className: "unknown" };
    const items = Array.isArray(order.items) ? order.items : [];
    const itemsHtml = items.map((item) => `
      <div class="order-product-row">
        <div class="order-product-icon">▣</div>
        <div class="order-product-name"><strong>${escapeHtml(item.product_name || "Sản phẩm")}</strong><small>${Number(item.quantity) || 0} × ${money(item.unit_price)}</small></div>
        <strong class="order-product-subtotal">${money(item.subtotal)}</strong>
      </div>`).join("");

    let actions = "";
    if (order.status === "PENDING_CONFIRMATION") {
      actions += `<button class="primary-button order-action" data-action="confirm" data-id="${id}">Xác nhận đơn hàng</button>`;
      actions += `<button class="danger-button order-action" data-action="cancel" data-id="${id}">Hủy đơn</button>`;
    } else if (order.status === "CONFIRMED") {
      if (order.payment_method === "COD") {
        actions += `<span class="order-cod-note">Thanh toán khi nhận hàng (COD)</span>`;
      } else {
        actions += `<button class="primary-button order-action" data-action="pay" data-id="${id}" data-result="SUCCESS">Thanh toán thành công (demo)</button>`;
        actions += `<button class="secondary-button order-action" data-action="pay" data-id="${id}" data-result="FAILED">Mô phỏng thanh toán lỗi</button>`;
      }
      actions += `<button class="danger-button order-action" data-action="cancel" data-id="${id}">Hủy đơn</button>`;
    } else if (order.status === "PAID") {
      actions = `<span class="order-complete-note">✓ Đơn hàng đã thanh toán, không thể hủy.</span>`;
    } else if (order.status === "CANCELLED") {
      actions = `<span class="order-complete-note">Đơn hàng đã được hủy.</span>`;
    }

    return `<article class="order-card" id="order-${id}">
      <div class="order-card-head"><div><span class="section-kicker">MÃ ĐƠN HÀNG</span><h2>#${id}</h2><p>Đặt lúc ${formatDate(order.created_at)}</p></div><span class="order-status-badge ${status.className}">${escapeHtml(status.label)}</span></div>
      <div class="order-customer"><div><span>Người nhận</span><strong>${escapeHtml(order.customer_name)}</strong></div><div><span>Số điện thoại</span><strong>${escapeHtml(order.phone)}</strong></div><div class="order-address"><span>Địa chỉ giao hàng</span><strong>${escapeHtml(order.address)}</strong></div></div>
      <div class="order-products">${itemsHtml || '<p>Không có thông tin sản phẩm.</p>'}</div>
      <div class="order-card-bottom"><div class="order-payment-method"><span>Phương thức</span><strong>${order.payment_method === "COD" ? "Thanh toán khi nhận hàng (COD)" : "Thanh toán mô phỏng"}</strong></div><div class="order-total"><span>Tổng đơn hàng</span><strong>${money(order.total_amount)}</strong></div></div>
      <div class="order-card-actions">${actions}</div>
      <p class="order-message" id="order-message-${id}" role="status" aria-live="polite"></p>
    </article>`;
  }).join("");
}

async function loadOrders() {
  statusLine.textContent = "Đang tải đơn hàng...";
  orderList.innerHTML = "";
  try {
    const orders = await api("/api/orders");
    renderOrders(orders);
    statusLine.textContent = `Có ${orders.length} đơn hàng.`;
  } catch (error) {
    statusLine.textContent = `Không thể tải đơn hàng: ${error.message}`;
    orderList.innerHTML = `<section class="orders-error"><h2>Chưa tải được dữ liệu</h2><p>${escapeHtml(error.message)}</p><button class="secondary-button" type="button" id="retry-orders">Thử lại</button></section>`;
    document.getElementById("retry-orders")?.addEventListener("click", loadOrders);
  }
}

orderList.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button || button.disabled) return;
  const id = Number(button.dataset.id);
  const action = button.dataset.action;
  const message = document.getElementById(`order-message-${id}`);
  if (action === "cancel" && !window.confirm(`Bạn có chắc muốn hủy đơn hàng #${id}?`)) return;

  let url = `/api/orders/${id}`;
  let options = { method: "POST" };
  let loadingText = "Đang xử lý...";
  if (action === "confirm") { url += "/confirm"; loadingText = "Đang xác nhận..."; }
  if (action === "cancel") { url += "/cancel"; loadingText = "Đang hủy đơn..."; }
  if (action === "pay") {
    url += "/pay";
    options.body = JSON.stringify({ simulate_result: button.dataset.result });
    loadingText = "Đang xử lý thanh toán...";
  }

  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = loadingText;
  if (message) { message.textContent = ""; message.className = "order-message"; }
  try {
    const result = await api(url, options);
    if (message) {
      message.textContent = result.message || "Thao tác thành công.";
      message.classList.add("success");
    }
    await loadOrders();
    const updatedMessage = document.getElementById(`order-message-${id}`);
    if (updatedMessage && result.message) {
      updatedMessage.textContent = result.message;
      updatedMessage.classList.add(action === "pay" && button.dataset.result === "FAILED" ? "warning" : "success");
    }
  } catch (error) {
    if (message) { message.textContent = error.message; message.classList.add("error"); }
    button.disabled = false;
    button.textContent = originalText;
  }
});

document.getElementById("refresh-orders").addEventListener("click", loadOrders);
document.getElementById("year").textContent = new Date().getFullYear();
loadOrders();
