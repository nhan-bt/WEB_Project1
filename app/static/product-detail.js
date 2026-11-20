const money = (value) => new Intl.NumberFormat("vi-VN", {
  style: "currency", currency: "VND", maximumFractionDigits: 0
}).format(Number(value) || 0);

const statusEl = document.getElementById("product-detail-status");
const contentEl = document.getElementById("product-detail-content");
const relatedEl = document.getElementById("related-products-grid");
const cartCountEl = document.getElementById("cart-count");
const toastEl = document.getElementById("toast");
const productId = Number(new URLSearchParams(window.location.search).get("id"));
let product = null;
let allProducts = [];
let quantity = 1;
let toastTimer;

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[char]));
}

const PRODUCT_CONTENT = [
  {
    match: /áo|shirt|\bthun\b/i,
    category: "Thời trang",
    image: "/static/images/ao_thun_01.avif",
    description: "Áo thun basic thiết kế tối giản, dễ phối cùng quần jean, quần short hoặc trang phục thường ngày. Phù hợp đi học, đi chơi và mặc hằng ngày.",
    details: [
      "Kiểu dáng: áo thun cổ tròn, tay ngắn",
      "Phong cách: tối giản, dễ phối đồ",
      "Bảo quản: giặt với màu tương tự, tránh chất tẩy mạnh"
    ]
  },
  {
    match: /quần|quan|jean|denim|pants/i,
    category: "Thời trang",
    image: "/static/images/quan_jean_01.avif",
    description: "Quần jean phong cách casual, phù hợp phối với áo thun hoặc sơ mi. Chất liệu denim và đường may tạo vẻ ngoài năng động cho trang phục thường ngày.",
    details: [
      "Loại sản phẩm: quần jean denim",
      "Phong cách: casual, năng động",
      "Bảo quản: lộn trái khi giặt để giữ màu denim"
    ]
  },
  {
    match: /giày|giay|sneaker|shoe|\bthể thao\b|\bthe thao\b/i,
    category: "Giày dép",
    image: "/static/images/giay_the_thao_01.jpg",
    description: "Giày thể thao kiểu dáng năng động, có thể kết hợp với quần jean, quần jogger hoặc trang phục đi chơi. Thiết kế hướng đến sự tiện dụng trong sinh hoạt hằng ngày.",
    details: [
      "Loại sản phẩm: giày thể thao",
      "Phong cách: năng động, thường ngày",
      "Bảo quản: vệ sinh bằng khăn mềm và để khô tự nhiên"
    ]
  }
];

function productVisual(item, index = 0) {
  const name = String(item?.name || "");
  const matched = PRODUCT_CONTENT.find((entry) => entry.match.test(name));
  if (matched) return { ...matched, symbol: "", visualClass: `visual-${(index % 3) + 1}` };
  return {
    category: "Sản phẩm tiện ích",
    image: "https://images.unsplash.com/photo-1441986300917-64674bd600d8?auto=format&fit=crop&w=1200&q=85",
    description: "Sản phẩm được quản lý trực tiếp từ hệ thống BlueMart. Giá bán và tồn kho được lấy từ dữ liệu hiện có.",
    details: ["Thông tin giá và tồn kho lấy từ hệ thống", "Vui lòng kiểm tra thông tin trước khi đặt hàng"],
    symbol: "", visualClass: `visual-${(index % 3) + 1}`
  };
}

async function requestJson(url, options = {}) {
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
  toastEl.textContent = message;
  toastEl.classList.toggle("error", isError);
  toastEl.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.remove("show"), 2800);
}

async function refreshCartCount() {
  try {
    const cart = await requestJson("/api/cart");
    cartCountEl.textContent = Number(cart.total_quantity) || 0;
  } catch { cartCountEl.textContent = "0"; }
}

function renderProduct(item) {
  const visual = productVisual(item);
  const stock = Math.max(0, Number(item.stock) || 0);
  const soldOut = stock <= 0;
  document.title = `${item.name} | BlueMart`;
  document.getElementById("breadcrumb-product").textContent = item.name;
  statusEl.textContent = "";
  contentEl.innerHTML = `
    <div class="product-detail-gallery">
      <div class="product-detail-main-visual ${visual.visualClass} product-photo-wrap">
        <span class="product-detail-badge">${soldOut ? "TẠM HẾT HÀNG" : "SẢN PHẨM CỬA HÀNG"}</span>
        <img class="product-detail-photo" src="${visual.image}" alt="Ảnh sản phẩm ${escapeHtml(item.name)}" loading="eager" referrerpolicy="no-referrer">
      </div>
      <p class="detail-image-note">Ảnh sản phẩm tham khảo từ Unsplash; có thể thay bằng ảnh thực tế của cửa hàng.</p>
    </div>
    <div class="product-detail-info">
      <div class="detail-category"><span class="detail-category-dot"></span>${visual.category}</div>
      <h1>${escapeHtml(item.name)}</h1>
      <p class="detail-subtitle">${escapeHtml(visual.description)}</p>
      <section class="detail-description"><h2>Mô tả chi tiết</h2><ul>${visual.details.map((line) => `<li>${escapeHtml(line)}</li>`).join("")}</ul><p class="detail-description-note">Lưu ý: mô tả hiện là nội dung giới thiệu mẫu theo nhóm sản phẩm. Màu sắc, kích cỡ và thông số cụ thể cần được xác nhận theo hàng thực tế.</p></section>
      <div class="detail-price-panel"><span>Giá bán</span><strong>${money(item.price)}</strong><small>Đã bao gồm giá niêm yết trên hệ thống</small></div>
      <div class="detail-stock-row"><span class="detail-stock-dot ${soldOut ? "out" : ""}"></span><strong>${soldOut ? "Tạm hết hàng" : "Còn hàng"}</strong><span>${soldOut ? "Vui lòng quay lại sau" : `Còn ${stock} sản phẩm`}</span></div>
      <div class="detail-divider"></div>
      <label class="detail-quantity-label" for="detail-quantity">Số lượng</label>
      <div class="detail-buy-row">
        <div class="detail-quantity-control"><button id="quantity-minus" type="button" aria-label="Giảm số lượng" ${soldOut ? "disabled" : ""}>−</button><output id="detail-quantity">1</output><button id="quantity-plus" type="button" aria-label="Tăng số lượng" ${soldOut ? "disabled" : ""}>+</button></div>
        <span class="detail-quantity-help">Tối đa ${stock} sản phẩm</span>
      </div>
      <p class="detail-inline-error" id="detail-inline-error" role="status"></p>
      <div class="detail-actions"><button class="primary-button detail-add-button" id="detail-add-to-cart" type="button" ${soldOut ? "disabled" : ""}>Thêm vào giỏ hàng <span aria-hidden="true">→</span></button><button class="secondary-button detail-buy-button" id="detail-buy-now" type="button" ${soldOut ? "disabled" : ""}>Mua ngay</button></div>
      <div class="detail-trust-list"><div><span>✓</span> Giá bán minh bạch</div><div><span>✓</span> Kiểm tra tồn kho trước khi thêm giỏ</div><div><span>✓</span> Thanh toán mô phỏng phục vụ học tập</div></div>
    </div>`;

  document.getElementById("quantity-minus").addEventListener("click", () => setQuantity(quantity - 1));
  document.getElementById("quantity-plus").addEventListener("click", () => setQuantity(quantity + 1));
  document.getElementById("detail-add-to-cart").addEventListener("click", () => addToCart(false));
  document.getElementById("detail-buy-now").addEventListener("click", () => addToCart(true));
}

function setQuantity(next) {
  const maxStock = Math.max(0, Number(product?.stock) || 0);
  const error = document.getElementById("detail-inline-error");
  if (next > maxStock) {
    if (error) error.textContent = `Chỉ còn ${maxStock} sản phẩm trong kho.`;
    return;
  }
  quantity = Math.max(1, next);
  document.getElementById("detail-quantity").textContent = quantity;
  if (error) error.textContent = "";
}

async function addToCart(buyNow) {
  const addButton = document.getElementById("detail-add-to-cart");
  const buyButton = document.getElementById("detail-buy-now");
  const clicked = buyNow ? buyButton : addButton;
  if (!product || quantity < 1 || quantity > Number(product.stock)) return;
  const oldText = clicked.textContent;
  addButton.disabled = true;
  buyButton.disabled = true;
  clicked.textContent = "Đang xử lý...";
  try {
    if (buyNow) {
      // Mua ngay đặt số lượng của sản phẩm này bằng số đã chọn, không cộng dồn lần nữa.
      // Nếu sản phẩm đã có trong giỏ, PATCH thay thế số lượng; nếu chưa có, POST thêm mới.
      const cart = await requestJson("/api/cart");
      const existingItem = (cart.items || []).find(
        (item) => Number(item.product_id) === Number(product.id)
      );
      if (existingItem) {
        await requestJson(`/api/cart/items/${Number(product.id)}`, {
          method: "PATCH",
          body: JSON.stringify({ quantity })
        });
      } else {
        await requestJson("/api/cart/items", {
          method: "POST",
          body: JSON.stringify({ product_id: Number(product.id), quantity })
        });
      }
      await refreshCartCount();
      window.location.href = "/static/checkout.html";
      return;
    }

    await requestJson("/api/cart/items", {
      method: "POST",
      body: JSON.stringify({ product_id: Number(product.id), quantity })
    });
    await refreshCartCount();
    showToast(`Đã thêm ${quantity} “${product.name}” vào giỏ hàng.`);
    quantity = 1;
    document.getElementById("detail-quantity").textContent = "1";
  } catch (error) {
    const inlineError = document.getElementById("detail-inline-error");
    if (inlineError) inlineError.textContent = error.message;
    showToast(error.message, true);
  } finally {
    addButton.disabled = Number(product.stock) <= 0;
    buyButton.disabled = Number(product.stock) <= 0;
    addButton.innerHTML = 'Thêm vào giỏ hàng <span aria-hidden="true">→</span>';
    buyButton.textContent = "Mua ngay";
    if (clicked === addButton && !buyNow) addButton.innerHTML = 'Thêm vào giỏ hàng <span aria-hidden="true">→</span>';
  }
}

function renderRelatedProducts(items) {
  const related = items.filter((item) => Number(item.id) !== productId).slice(0, 3);
  if (!related.length) {
    relatedEl.innerHTML = '<p class="muted">Hiện chưa có sản phẩm khác để gợi ý.</p>';
    return;
  }
  relatedEl.innerHTML = related.map((item, index) => {
    const visual = productVisual(item, index);
    const soldOut = Number(item.stock) <= 0;
    return `<article class="related-product-card"><a class="related-product-visual ${visual.visualClass}" href="/static/product.html?id=${Number(item.id)}" aria-label="Xem ${escapeHtml(item.name)}"><img src="${visual.image}" alt="Ảnh ${escapeHtml(item.name)}" loading="lazy" referrerpolicy="no-referrer"></a><div class="related-product-info"><span>${visual.category}</span><a href="/static/product.html?id=${Number(item.id)}"><strong>${escapeHtml(item.name)}</strong></a><b>${money(item.price)}</b><small class="${soldOut ? "related-sold-out" : ""}">${soldOut ? "Hết hàng" : `Còn ${Number(item.stock)} sản phẩm`}</small></div></article>`;
  }).join("");
}

async function init() {
  document.getElementById("year").textContent = new Date().getFullYear();
  refreshCartCount();
  if (!Number.isInteger(productId) || productId <= 0) {
    statusEl.textContent = "Không tìm thấy mã sản phẩm. Vui lòng quay lại trang chủ và chọn sản phẩm.";
    contentEl.innerHTML = '<a class="primary-button" href="/#products">Quay lại danh sách sản phẩm</a>';
    return;
  }
  try {
    const results = await Promise.all([
      requestJson(`/api/products/${productId}`),
      requestJson("/api/products")
    ]);
    product = results[0];
    allProducts = results[1];
    renderProduct(product);
    renderRelatedProducts(allProducts);
  } catch (error) {
    statusEl.textContent = error.message.includes("404") || error.message.toLowerCase().includes("not found")
      ? "Sản phẩm không tồn tại hoặc đã bị xóa."
      : `Không thể tải sản phẩm: ${error.message}`;
    contentEl.innerHTML = '<a class="primary-button" href="/#products">Quay lại danh sách sản phẩm</a>';
  }
}

init();
