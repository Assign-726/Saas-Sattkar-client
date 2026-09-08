// Global state
let currentOrder = {
    items: [],
    customer_name: '',
    phone: '',
    table_number: 1
};

// LocalStorage helpers for the cart page format (array of items)
function getSavedCartArray() {
    try {
        return JSON.parse(localStorage.getItem('restaurant_cart')) || [];
    } catch {
        return [];
    }
}
function setSavedCartArray(arr) {
    localStorage.setItem('restaurant_cart', JSON.stringify(arr));
}

// Prime sidebar order summary from saved cart array
// cart.html expects: [{ id, name, price, quantity, image? }]
// sidebar expects:   [{ menu_item, menu_item_name, quantity, unit_price }]
(() => {
    const arr = getSavedCartArray();
    if (Array.isArray(arr) && arr.length) {
        currentOrder.items = arr.map(it => ({
            menu_item: it.id,
            menu_item_name: it.name,
            quantity: it.quantity || 1,
            unit_price: parseFloat(it.price) || 0,
        }));
    }
})();

// DOM Elements
const menuItemsContainer = document.getElementById('menu-items-container');
const orderItemsContainer = document.getElementById('order-items');
const orderTotalElement = document.getElementById('order-total');
const checkoutBtn = document.getElementById('checkout-btn');

// Initialize the app
document.addEventListener('DOMContentLoaded', () => {
    loadCategories();
    loadMenuItems();
    updateOrderSummary(); // Update cart display on page load
});

// API Functions
async function loadCategories() {
    try {
        const response = await axios.get('/api/categories/');
        const categoriesContainer = document.getElementById('categories-container');
        categoriesContainer.innerHTML = `
            <div class="btn-group" role="group">
                ${response.data.map(category => `
                    <button type="button" class="btn btn-outline-primary" 
                            onclick="loadSubcategories(${category.id})">
                        ${category.name}
                    </button>
                `).join('')}
            </div>
        `;
    } catch (error) {
        console.error('Error loading categories:', error);
        alert('Error loading categories');
    }
}

async function loadSubcategories(categoryId) {
    try {
        const response = await axios.get(`/api/subcategories/?category=${categoryId}`);
        const categoriesContainer = document.getElementById('categories-container');
        categoriesContainer.innerHTML += `
            <div class="btn-group mt-2" role="group">
                ${response.data.map(subcategory => `
                    <button type="button" class="btn btn-outline-secondary" 
                            onclick="loadMenuItems(${subcategory.id})">
                        ${subcategory.name}
                    </button>
                `).join('')}
            </div>
        `;
    } catch (error) {
        console.error('Error loading subcategories:', error);
        alert('Error loading subcategories');
    }
}

async function loadMenuItems(categoryId = null) {
    try {
        let url = '/api/menu-items/';
        if (categoryId) {
            url += `?category=${categoryId}`;
        }
        
        const response = await axios.get(url);
        menuItemsContainer.innerHTML = response.data.map(item => `
            <div class="col-md-4">
                <div class="card menu-item-card">
                    <div class="card-body">
                        <h5 class="card-title">${item.name}</h5>
                        <p class="card-text">${item.description || 'No description'}</p>
                        <div class="d-flex justify-content-between align-items-center">
                            <span class="h5 mb-0">$${parseFloat(item.price).toFixed(2)}</span>
                            <button class="btn btn-primary btn-sm" 
                                    onclick="addToOrder(${item.id}, ${JSON.stringify(item.name)}, ${item.price})">
                                Add to Order
                            </button>
                        </div>
                    </div>
                </div>
            </div>
        `).join('');
    } catch (error) {
        console.error('Error loading menu items:', error);
        alert('Error loading menu items');
    }
}

// Order Management
function addToOrder(itemId, itemName, price) {
    // Update saved cart array (format used by cart.html)
    const cartArr = getSavedCartArray();
    const existing = cartArr.find(i => i.id === itemId);
    if (existing) {
        existing.quantity = (existing.quantity || 1) + 1;
    } else {
        cartArr.push({ id: itemId, name: itemName, price: parseFloat(price) || 0, quantity: 1 });
    }
    setSavedCartArray(cartArr);

    // Keep sidebar summary in sync
    const existingItem = currentOrder.items.find(item => item.menu_item === itemId);
    if (existingItem) {
        existingItem.quantity += 1;
    } else {
        currentOrder.items.push({
            menu_item: itemId,
            menu_item_name: itemName,
            quantity: 1,
            unit_price: parseFloat(price) || 0,
        });
    }

    updateOrderSummary();
    
    // Show feedback to user
    const toast = document.createElement('div');
    toast.className = 'position-fixed bottom-0 end-0 m-3 p-3 bg-success text-white rounded';
    toast.style.zIndex = '1100';
    toast.textContent = `${itemName} added to cart!`;
    document.body.appendChild(toast);
    
    // Remove toast after 2 seconds
    setTimeout(() => {
        toast.remove();
    }, 2000);
}

function updateOrderSummary() {
    if (!orderItemsContainer) return; // Skip if not on a page with order summary
    
    if (currentOrder.items.length === 0) {
        orderItemsContainer.innerHTML = '<p class="text-muted">Your cart is empty</p>';
    } else {
        orderItemsContainer.innerHTML = currentOrder.items.map(item => `
            <div class="d-flex justify-content-between mb-2">
                <div>
                    ${item.quantity}x ${item.menu_item_name}
                    <button class="btn btn-sm btn-outline-danger ms-2" 
                            onclick="removeFromOrder(${item.menu_item})">
                        ×
                    </button>
                </div>
                <div>$${(item.quantity * item.unit_price).toFixed(2)}</div>
            </div>
        `).join('');
    }

    const total = currentOrder.items.reduce((sum, item) => sum + (item.quantity * item.unit_price), 0);
    if (orderTotalElement) {
        orderTotalElement.textContent = `$${total.toFixed(2)}`;
    }
    
    if (checkoutBtn) {
        checkoutBtn.disabled = currentOrder.items.length === 0;
    }
    
    // Update cart count in navbar if it exists
    const cartCount = document.getElementById('cart-count');
    if (cartCount) {
        const totalItems = currentOrder.items.reduce((sum, item) => sum + item.quantity, 0);
        cartCount.textContent = totalItems;
        cartCount.style.display = totalItems > 0 ? 'inline-block' : 'none';
    }
}

function removeFromOrder(itemId) {
    // Remove from saved cart array
    const cartArr = getSavedCartArray().filter(i => i.id !== itemId);
    setSavedCartArray(cartArr);

    // Remove from sidebar order summary
    currentOrder.items = currentOrder.items.filter(item => item.menu_item !== itemId);
    updateOrderSummary();
    
    // Show feedback to user
    const toast = document.createElement('div');
    toast.className = 'position-fixed bottom-0 end-0 m-3 p-3 bg-danger text-white rounded';
    toast.style.zIndex = '1100';
    toast.textContent = 'Item removed from cart';
    document.body.appendChild(toast);
    
    // Remove toast after 2 seconds
    setTimeout(() => {
        toast.remove();
    }, 2000);
}

// Checkout
async function checkout() {
    const customerName = prompt('Please enter your name:');
    if (!customerName) return;

    const phone = prompt('Please enter your phone number:');
    if (!phone) return;

    const tableNumber = prompt('Please enter your table number:', '1');
    if (!tableNumber) return;

    currentOrder.customer_name = customerName;
    currentOrder.phone = phone;
    currentOrder.table_number = parseInt(tableNumber);

    try {
        const response = await axios.post('/api/orders/', currentOrder);
        alert('Order placed successfully!');
        currentOrder.items = [];
        updateOrderSummary();
    } catch (error) {
        console.error('Error placing order:', error);
        alert('Error placing order: ' + (error.response?.data?.detail || error.message));
    }
}

// View Orders
async function viewOrders() {
    try {
        const response = await axios.get('/api/orders/');
        const orders = response.data;
        
        const orderDetails = document.getElementById('order-details');
        orderDetails.style.display = 'block';
        orderDetails.innerHTML = `
            <h3>Recent Orders</h3>
            <div class="list-group">
                ${orders.map(order => `
                    <div class="list-group-item">
                        <div class="d-flex justify-content-between">
                            <h5>Order #${order.id}</h5>
                            <span class="badge bg-${getStatusBadgeClass(order.status)}">
                                ${order.status_display}
                            </span>
                        </div>
                        <p class="mb-1">Customer: ${order.customer_name}</p>
                        <p class="mb-1">Table: ${order.table_number}</p>
                        <p class="mb-1">Total: $${parseFloat(order.total_amount).toFixed(2)}</p>
                        <button class="btn btn-sm btn-info" onclick="viewOrderDetails(${order.id})">
                            View Details
                        </button>
                    </div>
                `).join('')}
            </div>
        `;
        
        // Scroll to order details
        orderDetails.scrollIntoView({ behavior: 'smooth' });
    } catch (error) {
        console.error('Error loading orders:', error);
        alert('Error loading orders');
    }
}

async function viewOrderDetails(orderId) {
    try {
        const response = await axios.get(`/api/orders/${orderId}/receipt/`);
        const order = response.data;
        
        const modalBody = document.getElementById('order-modal-body');
        modalBody.innerHTML = `
            <h5>Order #${order.id}</h5>
            <p><strong>Customer:</strong> ${order.customer_name}</p>
            <p><strong>Table:</strong> ${order.table_number}</p>
            <p><strong>Status:</strong> ${order.status_display}</p>
            <p><strong>Order Time:</strong> ${new Date(order.order_time).toLocaleString()}</p>
            
            <h6 class="mt-4">Items:</h6>
            <table class="table">
                <thead>
                    <tr>
                        <th>Item</th>
                        <th>Qty</th>
                        <th>Price</th>
                        <th>Total</th>
                    </tr>
                </thead>
                <tbody>
                    ${order.items.map(item => `
                        <tr>
                            <td>${item.menu_item_name}</td>
                            <td>${item.quantity}</td>
                            <td>$${parseFloat(item.unit_price).toFixed(2)}</td>
                            <td>$${(item.quantity * item.unit_price).toFixed(2)}</td>
                        </tr>
                    `).join('')}
                </tbody>
                <tfoot>
                    <tr>
                        <th colspan="3" class="text-end">Total:</th>
                        <th>$${parseFloat(order.total_amount).toFixed(2)}</th>
                    </tr>
                </tfoot>
            </table>
        `;
        
        const modal = new bootstrap.Modal(document.getElementById('orderModal'));
        modal.show();
    } catch (error) {
        console.error('Error loading order details:', error);
        alert('Error loading order details');
    }
}

function getStatusBadgeClass(status) {
    switch(status) {
        case 'P': return 'warning';
        case 'S': return 'success';
        case 'C': return 'danger';
        default: return 'secondary';
    }
}