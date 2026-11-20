const money = (value) => new Intl.NumberFormat("vi-VN", {
  style: "currency", currency: "VND", maximumFractionDigits: 0
}).format(Number(value) || 0);
const content = document.getElementById("checkout-content");
const statusBox = document.getElementById("checkout-status");

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[char]));
}
async function api(url, options = {}) {
  const response = await fetch(url, { ...options, headers: { "Content-Type": "application/json", ...(options.headers || {}) } });
  let data = {};
  try { data = await response.json(); } catch { /* empty response */ }
  if (!response.ok) throw new Error(data.detail || data.message || `Yêu cầu thất bại (${response.status})`);
  return data;
}
function render(cart) {
  if (!cart.items || !cart.items.length) {
    statusBox.textContent = "Giỏ hàng trống — chưa thể đặt hàng.";
    content.innerHTML = '<section class="cart-empty-page"><div class="cart-empty-illustration">🛒</div><h2>Không có sản phẩm để đặt</h2><p>Hãy thêm sản phẩm vào giỏ hàng trước khi tiếp tục.</p><a class="primary-button" href="/">Quay lại mua sắm</a></section>';
    return;
  }
  statusBox.textContent = "Vui lòng kiểm tra thông tin trước khi đặt hàng.";
  content.innerHTML = `
    <form class="checkout-form-panel" id="checkout-form">
      <span class="section-kicker">THÔNG TIN NGƯỜI NHẬN</span><h2>Thông tin giao hàng</h2>
      <label class="form-field">Họ và tên<input name="customer_name" required minlength="2" maxlength="100" autocomplete="name" placeholder="Nguyễn Văn A"></label>
      <label class="form-field">Số điện thoại<input name="phone" required minlength="9" maxlength="15" autocomplete="tel" inputmode="tel" placeholder="0900000000"></label>
      <label class="form-field">Địa chỉ nhận hàng<textarea name="address" required minlength="5" maxlength="255" autocomplete="street-address" rows="3" placeholder="Số nhà, đường, phường/xã, tỉnh/thành phố"></textarea></label>
      <label class="form-field">Phương thức thanh toán<select name="payment_method"><option value="MOCK">Thanh toán mô phỏng (demo)</option><option value="COD">Thanh toán khi nhận hàng (COD)</option></select></label>
      <p class="checkout-disclaimer">Đây là website demo. Chưa có kết nối ngân hàng hoặc cổng thanh toán thật.</p>
      <button class="primary-button checkout-submit" type="submit">Xác nhận đặt hàng <span>→</span></button>
      <p id="form-error" class="form-error" role="alert"></p>
    </form>
    <aside class="order-summary"><span class="section-kicker">ĐƠN HÀNG CỦA BẠN</span><h2>Tóm tắt</h2>
      <div class="checkout-summary-items">${cart.items.map(item => `<div class="checkout-summary-item"><span><strong>${escapeHtml(item.name)}</strong><small>${Number(item.quantity)} × ${money(item.price)}</small></span><b>${money(item.subtotal)}</b></div>`).join("")}</div>
      <div class="summary-row"><span>Tổng số lượng</span><strong>${Number(cart.total_quantity) || 0}</strong></div>
      <div class="summary-total"><span>Tổng tiền</span><strong>${money(cart.total_amount)}</strong></div>
    </aside>`;
  document.getElementById("checkout-form").addEventListener("submit", submitOrder);
}
async function loadCart() {
  try { render(await api("/api/cart")); }
  catch (error) { statusBox.textContent = `Không thể tải giỏ hàng: ${error.message}`; }
}
async function submitOrder(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('button[type="submit"]');
  const errorBox = document.getElementById("form-error");
  const formData = new FormData(form);
  const payload = {
    customer_name: String(formData.get("customer_name")).trim(),
    phone: String(formData.get("phone")).trim(),
    address: String(formData.get("address")).trim(),
    payment_method: String(formData.get("payment_method"))
  };
  button.disabled = true; button.textContent = "Đang tạo đơn hàng..."; errorBox.textContent = "";
  try {
    const result = await api("/api/orders", { method: "POST", body: JSON.stringify(payload) });
    const order = result.order;
    content.innerHTML = `<section class="order-success"><div class="success-icon">✓</div><span class="section-kicker">ĐẶT HÀNG THÀNH CÔNG</span><h2>Cảm ơn bạn đã đặt hàng!</h2><p>Đơn hàng <strong>#${Number(order.id)}</strong> đã được tạo.</p><div class="summary-total"><span>Tổng tiền</span><strong>${money(order.total_amount)}</strong></div><p>Trạng thái: <strong>${escapeHtml(order.status || "PENDING_CONFIRMATION")}</strong></p><a class="primary-button" href="/static/orders.html">Theo dõi / thanh toán đơn hàng</a> <a class="secondary-button" href="/">Tiếp tục mua sắm</a></section>`;
    statusBox.textContent = "Đơn hàng đã được tạo thành công.";
  } catch (error) {
    errorBox.textContent = error.message;
    button.disabled = false; button.innerHTML = 'Xác nhận đặt hàng <span>→</span>';
  }
}
document.getElementById("year").textContent = new Date().getFullYear();
loadCart();
