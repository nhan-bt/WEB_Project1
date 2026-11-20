const API_BASE = ""; // Same origin: keep using the existing FastAPI routes.
const productGrid = document.getElementById("product-grid");
const productStatus = document.getElementById("product-status");
const searchInput = document.getElementById("product-search");
const emptyState = document.getElementById("empty-state");
const cartCount = document.getElementById("cart-count");
const cartModal = document.getElementById("cart-modal");
const cartModalContent = document.getElementById("cart-modal-content");
const cartModalTotal = document.getElementById("cart-modal-total");
const toast = document.getElementById("toast");

let allProducts = [];
let toastTimer;

const money = (value) => new Intl.NumberFormat("vi-VN", {
  style: "currency", currency: "VND", maximumFractionDigits: 0
}).format(Number(value) || 0);

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[char]));
}

const PRODUCT_CONTENT = [
  {
    match: /áo|shirt|\bthun\b/i,
    category: "Thời trang",
    symbol: "👕",
    image: "/static/images/ao_thun_01.avif",
    description: "Áo thun basic tối giản, dễ phối đồ hằng ngày."
  },
  {
    match: /quần|quan|jean|denim|pants/i,
    category: "Thời trang",
    symbol: "👖",
    image: "/static/images/quan_jean_01.avif",
    description: "Quần jean phong cách casual, năng động và dễ phối."
  },
  {
  match: /giày|giay|sneaker|shoe|\bthể thao\b|\bthe thao\b/i,
  category: "Giày dép",
  symbol: "👟",
  image: "/static/images/giay_the_thao_01.jpg",
  description: "Giày thể thao kiểu dáng năng động cho sinh hoạt hằng ngày."
}
];

function productVisual(product, index) {
  const name = String(product.name || "");
  const matched = PRODUCT_CONTENT.find((entry) => entry.match.test(name));
  return matched || { category: "Sản phẩm tiện ích", symbol: "🛍️", image: "https://images.unsplash.com/photo-1441986300917-64674bd600d8?auto=format&fit=crop&w=900&q=85", description: "Sản phẩm được quản lý trực tiếp từ hệ thống cửa hàng." , visualClass: `visual-${(index % 3) + 1}` };
}

function renderProducts(products) {
  productGrid.innerHTML = "";
  emptyState.classList.toggle("hidden", products.length > 0);
  if (!products.length) return;

  productGrid.innerHTML = products.map((product, index) => {
    const visual = productVisual(product, index);
    const stock = Number(product.stock) || 0;
    const soldOut = stock <= 0;
    const badge = index === 0 ? "ĐƯỢC QUAN TÂM" : (stock > 0 ? "CÒN HÀNG" : "TẠM HẾT HÀNG");
    return `
      <article class="product-card">
        <div class="product-visual ${visual.visualClass || `visual-${(index % 3) + 1}`}" >
          <span class="product-badge">${badge}</span>
          <img class="product-card-photo" src="${visual.image}" alt="Ảnh ${escapeHtml(product.name)}" loading="lazy" referrerpolicy="no-referrer">
        </div>
        <div class="product-info">
          <span class="product-category">${visual.category}</span>
          <h3 class="product-name"><a class="product-detail-link" href="/static/product.html?id=${Number(product.id)}">${escapeHtml(product.name)}</a></h3>
          <p class="product-description">${escapeHtml(visual.description)}</p>
          <div class="product-meta">
            <strong class="product-price">${money(product.price)}</strong>
            <span class="stock-label ${soldOut ? "out" : ""}">${soldOut ? "Hết hàng" : `Còn ${stock} sản phẩm`}</span>
          </div>
          <div class="product-card-actions">
            <a class="detail-button" href="/static/product.html?id=${Number(product.id)}">Xem chi tiết</a>
            <button class="add-button" type="button" data-add-product="${Number(product.id)}" ${soldOut ? "disabled" : ""}>
              ${soldOut ? "Tạm hết hàng" : "+ Thêm vào giỏ"}
            </button>
          </div>
        </div>
      </article>`;
  }).join("");
}

async function requestJson(url, options = {}) {
  const response = await fetch(`${API_BASE}${url}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) }
  });
  let data = {};
  try { data = await response.json(); } catch { /* response may be empty */ }
  if (!response.ok) {
    throw new Error(data.detail || data.message || `Yêu cầu thất bại (${response.status})`);
  }
  return data;
}

async function loadProducts() {
  productStatus.textContent = "Đang tải sản phẩm...";
  try {
    allProducts = await requestJson("/api/products");
    productStatus.textContent = `${allProducts.length} sản phẩm đang được quản lý bởi cửa hàng`;
    applySearch();
  } catch (error) {
    productStatus.textContent = "Không thể tải sản phẩm.";
    productGrid.innerHTML = `<div class="empty-state"><h3>Chưa kết nối được với máy chủ</h3><p>${escapeHtml(error.message)}. Hãy kiểm tra FastAPI đang chạy.</p></div>`;
  }
}

function applySearch() {
  const keyword = searchInput.value.trim().toLocaleLowerCase("vi-VN");
  const filtered = allProducts.filter((product) => String(product.name || "").toLocaleLowerCase("vi-VN").includes(keyword));
  renderProducts(filtered);
  if (allProducts.length && filtered.length) {
    productStatus.textContent = keyword ? `Tìm thấy ${filtered.length} sản phẩm` : `${allProducts.length} sản phẩm đang được quản lý bởi cửa hàng`;
  } else if (allProducts.length && !filtered.length) {
    productStatus.textContent = "Không có sản phẩm phù hợp với từ khóa.";
  }
}

function showToast(message, isError = false) {
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 2800);
}

async function refreshCartCount() {
  try {
    const cart = await requestJson("/api/cart");
    cartCount.textContent = Number(cart.total_quantity) || 0;
  } catch {
    cartCount.textContent = "0";
  }
}

async function addToCart(productId, button) {
  const product = allProducts.find((item) => Number(item.id) === productId);
  if (!product) return;
  if (button) { button.disabled = true; button.textContent = "Đang thêm..."; }
  try {
    await requestJson("/api/cart/items", {
      method: "POST",
      body: JSON.stringify({ product_id: productId, quantity: 1 })
    });
    await refreshCartCount();
    showToast(`Đã thêm “${product.name}” vào giỏ hàng.`);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    if (button) { button.disabled = Number(product.stock) <= 0; button.textContent = Number(product.stock) <= 0 ? "Tạm hết hàng" : "+ Thêm vào giỏ"; }
  }
}

async function openCart() {
  cartModal.classList.remove("hidden");
  document.body.style.overflow = "hidden";
  cartModalContent.innerHTML = '<p class="muted">Đang tải giỏ hàng...</p>';
  try {
    const cart = await requestJson("/api/cart");
    cartCount.textContent = Number(cart.total_quantity) || 0;
    cartModalTotal.textContent = money(cart.total_amount);
    if (!cart.items || !cart.items.length) {
      cartModalContent.innerHTML = '<div class="cart-empty"><span>🛒</span><strong>Giỏ hàng đang trống</strong><p>Hãy thêm một vài sản phẩm bạn yêu thích.</p></div>';
      return;
    }
    cartModalContent.innerHTML = `<div class="cart-preview-list">${cart.items.map((item, index) => {
      const visual = productVisual({ name: item.name }, index);
      return `<div class="cart-preview-item"><span class="cart-preview-icon">${visual.symbol}</span><span><strong>${escapeHtml(item.name)}</strong><small>${Number(item.quantity)} × ${money(item.price)}</small></span><span class="preview-price">${money(item.subtotal)}</span></div>`;
    }).join("")}</div>`;
  } catch (error) {
    cartModalContent.innerHTML = `<p class="muted">${escapeHtml(error.message)}</p>`;
  }
}

function closeCart() {
  cartModal.classList.add("hidden");
  document.body.style.overflow = "";
}

productGrid.addEventListener("click", (event) => {
  const button = event.target.closest("[data-add-product]");
  if (button) addToCart(Number(button.dataset.addProduct), button);
});
searchInput.addEventListener("input", applySearch);
document.getElementById("open-cart").addEventListener("click", openCart);
document.getElementById("close-cart").addEventListener("click", closeCart);
cartModal.addEventListener("click", (event) => { if (event.target === cartModal) closeCart(); });
document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeCart(); });
document.getElementById("year").textContent = new Date().getFullYear();

loadProducts();
refreshCartCount();
