/**
 * Основной JavaScript маркетплейса
 */

// Получение CSRF токена из meta тега
function getCsrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute('content') : '';
}

document.addEventListener('DOMContentLoaded', function() {
    // Инициализация flash сообщений
    initFlashMessages();

    // Инициализация форм
    initForms();

    // Инициализация кнопок избранного
    initFavoriteButtons();

    // Инициализация табов на странице товара (.tab-btn / .tab-panel)
    initProductTabs();
    // Инициализация кнопок корзины
    initCartButtons();
    
    // Маска для телефона
    initPhoneMask();
});


/**
 * Flash сообщения
 */
function initFlashMessages() {
    const messages = document.querySelectorAll('.flash-message');
    messages.forEach(msg => {
        setTimeout(() => {
            msg.style.opacity = '0';
            msg.style.transform = 'translateX(100%)';
            setTimeout(() => msg.remove(), 300);
        }, 5000);
    });
}


/**
 * Инициализация форм
 */
function initForms() {
    // Подтверждение удаления
    const deleteForms = document.querySelectorAll('form[data-confirm]');
    deleteForms.forEach(form => {
        form.addEventListener('submit', function(e) {
            if (!confirm(this.dataset.confirm || 'Вы уверены?')) {
                e.preventDefault();
            }
        });
    });
}


/**
 * Кнопки избранного
 */
function initFavoriteButtons() {
    // Ищем оба класса: .favorite-btn (карточки) и .btn-favorite (страница товара)
    const buttons = document.querySelectorAll('.favorite-btn, .btn-favorite');
    
    buttons.forEach(btn => {
        btn.addEventListener('click', async function(e) {
            e.preventDefault();
            e.stopPropagation();
            
            const productId = this.dataset.productId;
            const csrfToken = getCsrfToken();
            const isOnFavoritesPage = window.location.search.includes('section=favorite');
            
            try {
                const response = await fetch('/api/favorite/toggle', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/x-www-form-urlencoded',
                        'X-CSRFToken': csrfToken
                    },
                    body: `product_id=${productId}`
                });
                
                const data = await response.json();
                
                if (data.success) {
                    // Переключение состояния (CSS теперь управляет цветом)
                    if (data.in_favorite) {
                        this.classList.add('active');
                        // Обновляем текст кнопки на "В избранном"
                        const span = this.querySelector('span');
                        if (span) {
                            span.textContent = 'В избранном';
                        }
                    } else {
                        this.classList.remove('active');
                        // Обновляем текст кнопки на "В избранное"
                        const span = this.querySelector('span');
                        if (span) {
                            span.textContent = 'В избранное';
                        }
                        
                        // Если мы на странице избранного - удаляем карточку с анимацией
                        if (isOnFavoritesPage) {
                            removeFavoriteCard(this);
                        }
                    }
                    
                    // Уведомление
                    showNotification(data.message, 'success');
                } else {
                    showNotification(data.error || 'Ошибка', 'error');
                }
            } catch (error) {
                showNotification('Произошла ошибка', 'error');
            }
        });
    });
}


/**
 * Удаление карточки товара из избранного с анимацией
 */
function removeFavoriteCard(button) {
    // Находим родительскую карточку товара
    const productCard = button.closest('.product-card');
    if (!productCard) return;
    
    // Добавляем класс для анимации исчезновения
    productCard.classList.add('removing');
    
    // Анимация исчезновения
    productCard.style.opacity = '0';
    productCard.style.transform = 'scale(0.8)';
    productCard.style.margin = '0';
    productCard.style.padding = '0';
    
    // Удаляем из DOM после завершения анимации
    setTimeout(() => {
        productCard.remove();
        
        // Проверяем, остались ли еще карточки
        checkEmptyFavorites();
    }, 300);
}


/**
 * Проверка и отображение пустого состояния
 */
function checkEmptyFavorites() {
    const favoritesGrid = document.querySelector('.favorites-grid');
    if (!favoritesGrid) return;
    
    const remainingCards = favoritesGrid.querySelectorAll('.product-card');
    
    if (remainingCards.length === 0) {
        // Создаем элемент пустого состояния
        const emptyState = document.createElement('div');
        emptyState.className = 'empty-state';
        emptyState.innerHTML = `
            <div class="empty-icon">❤️</div>
            <h3>Ваше избранное пусто</h3>
            <p>Добавляйте товары, чтобы не потерять их</p>
            <a href="${window.location.origin}/" class="btn btn-primary">Перейти в каталог</a>
        `;
        
        // Добавляем стили для пустого состояния
        emptyState.style.cssText = `
            grid-column: 1 / -1;
            text-align: center;
padding: 60px 20px;
        `;
        
        // Удаляем все содержимое и добавляем пустое состояние
        favoritesGrid.innerHTML = '';
        favoritesGrid.appendChild(emptyState);
    }
}


/**
 * Кнопки корзины
 */
function initCartButtons() {
    // Кнопки добавления в корзину (несколько селекторов для совместимости)
    const addToCartBtns = document.querySelectorAll('.add-to-cart-btn, .btn-add-cart, .btn-quick-cart');
    
    addToCartBtns.forEach(btn => {
        btn.addEventListener('click', async function(e) {
            e.preventDefault();
            e.stopPropagation();
            
            const productId = this.dataset.productId;
            const quantity = this.dataset.quantity || 1;
            const csrfToken = getCsrfToken();
            const originalContent = this.innerHTML;
            
            try {
                // Показать состояние загрузки
                this.classList.add('loading');
                this.disabled = true;
                
                const response = await fetch('/api/cart/add', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/x-www-form-urlencoded',
                        'X-CSRFToken': csrfToken
                    },
                    body: `product_id=${productId}&quantity=${quantity}`
                });
                
                // Обработка 401 (не авторизован)
                if (response.status === 401) {
                    showNotification('Пожалуйста, войдите чтобы добавлять товары в корзину', 'error');
                    // Опционально: редирект на страницу входа
                    // window.location.href = '/auth/login?next=' + encodeURIComponent(window.location.pathname);
                    return;
                }
                
                const data = await response.json();
                
                if (data.success) {
                    // Обновление счётчика корзины в шапке
                    updateCartCount(data.cart_count);
                    
                    // Обновление UI страницы товара - заменяем кнопку на контролы
                    updateProductPageCartControls(productId, data.cart_quantity);
                    
                    // Обновление кнопок в карточках товаров
                    const cardBtn = document.querySelector(`.btn-add-cart[data-product-id="${productId}"], .btn-quick-cart[data-product-id="${productId}"]`);
                    if (cardBtn) {
                        cardBtn.classList.add('added');
                        cardBtn.innerHTML = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="3"><polyline points="20 6 9 17 4 12"></polyline></svg>';
                        setTimeout(() => {
                            cardBtn.classList.remove('added');
                            cardBtn.innerHTML = originalContent;
                        }, 1500);
                    }
                    
                    showNotification(data.message, 'success');
                } else {
                    this.classList.remove('loading');
                    this.disabled = false;
                    showNotification(data.error || 'Ошибка', 'error');
                }
            } catch (error) {
                console.error('Cart error:', error);
                this.classList.remove('loading');
                this.disabled = false;
                showNotification('Произошла ошибка при добавлении в корзину', 'error');
            }
        });
    });
    
    // Инициализация кнопок изменения количества (+/-)
    initCartQuantityControls();
}


/**
 * Обновление контролов корзины на странице товара
 */
function updateProductPageCartControls(productId, quantity) {
    // Находим кнопку добавления в корзину
    const addBtn = document.querySelector(`.add-to-cart-btn[data-product-id="${productId}"]`);
    if (!addBtn) return;
    
    // Находим родительский контейнер
    const parent = addBtn.parentElement;
    if (!parent) return;
    
    // Создаём HTML для контролов корзины
    const controlsHtml = `
        <div class="cart-controls">
            <button class="btn btn-quantity minus" data-product-id="${productId}">-</button>
            <span class="quantity-display">${quantity}</span>
            <button class="btn btn-quantity plus" data-product-id="${productId}">+</button>
            <a href="/cart" class="btn btn-primary">В корзине</a>
        </div>
    `;
    
    // Заменяем кнопку на контролы
    addBtn.remove();
    parent.insertAdjacentHTML('beforeend', controlsHtml);
    
    // Инициализируем новые кнопки +/- для AJAX обновления
    initCartQuantityControls();
}


/**
 * Инициализация кнопок изменения количества товара в корзине
 */
function initCartQuantityControls() {
    // Кнопки уменьшения количества
    document.querySelectorAll('.btn-quantity.minus').forEach(btn => {
        // Удаляем старые обработчики, чтобы не было дубликатов
        btn.replaceWith(btn.cloneNode(true));
    });
    
    // Кнопки увеличения количества
    document.querySelectorAll('.btn-quantity.plus').forEach(btn => {
        btn.replaceWith(btn.cloneNode(true));
    });
    
    // Добавляем новые обработчики
    document.querySelectorAll('.btn-quantity.minus').forEach(btn => {
        btn.addEventListener('click', async function() {
            const productId = this.dataset.productId;
            const csrfToken = getCsrfToken();
            
            // Находим текущее количество
            const display = this.parentElement.querySelector('.quantity-display');
            let currentQty = parseInt(display.textContent);
            
            if (currentQty <= 1) {
                // Если количество станет 0, удаляем товар из корзины
                await updateCartQuantity(productId, 0, csrfToken);
                // Обновляем UI - возвращаем кнопку "В корзину"
                resetToAddButton(productId);
            } else {
                // Уменьшаем количество на 1
                await updateCartQuantity(productId, currentQty - 1, csrfToken);
            }
        });
    });
    
    document.querySelectorAll('.btn-quantity.plus').forEach(btn => {
        btn.addEventListener('click', async function() {
            const productId = this.dataset.productId;
            const csrfToken = getCsrfToken();
            
            // Находим текущее количество
            const display = this.parentElement.querySelector('.quantity-display');
            let currentQty = parseInt(display.textContent);
            
            // Увеличиваем количество на 1
            await updateCartQuantity(productId, currentQty + 1, csrfToken);
        });
    });
}


/**
 * Обновление количества товара через AJAX
 */
async function updateCartQuantity(productId, quantity, csrfToken) {
    try {
        const response = await fetch('/api/cart/update', {
            method: 'POST',
headers: {
                'Content-Type': 'application/x-www-form-urlencoded',
                'X-CSRFToken': csrfToken
            },
            body: `product_id=${productId}&quantity=${quantity}`
        });
        
        const data = await response.json();
        
        if (data.success) {
            // Обновляем счётчик в шапке
            updateCartCount(data.cart_count);
            
            // Обновляем отображение количества на странице товара
            if (quantity > 0) {
                const controls = document.querySelector(`.cart-controls`);
                if (controls) {
                    const display = controls.querySelector('.quantity-display');
                    if (display) {
                        display.textContent = quantity;
                    }
                }
            }
        } else {
            showNotification(data.error || 'Ошибка', 'error');
        }
    } catch (error) {
        console.error('Cart update error:', error);
        showNotification('Произошла ошибка', 'error');
    }
}


/**
 * Возврат к кнопке "В корзину" (когда товар удалён из корзины)
 */
function resetToAddButton(productId) {
    const controls = document.querySelector(`.cart-controls`);
    if (!controls) return;
    
    const parent = controls.parentElement;
    
    const buttonHtml = `
        <button class="btn btn-primary add-to-cart-btn" data-product-id="${productId}" data-quantity="1">
            В корзину
        </button>
    `;
    
    controls.remove();
    parent.insertAdjacentHTML('beforeend', buttonHtml);
    
    // Инициализируем новую кнопку
    initCartButtons();
}


/**
 * Обновление счётчика корзины
 */
function updateCartCount(count) {
    const cartCount = document.querySelector('.cart-count');
    if (cartCount) {
        cartCount.textContent = count;
        cartCount.style.display = count > 0 ? 'block' : 'none';
    }
}


/**
 * Переключение вкладок на странице товара.
 * Кнопки имеют class="tab-btn" и data-tab="<panelId>",
 * панели — class="tab-panel" и id="<panelId>".
 * Активная кнопка/панель помечается классом "active".
 *
 * Раньше логика жила в inline-скрипте product.html — если в нём
 * выше по тексту ломалась JS-парсинг (например, в HTML попадал
 * "сырой" <, >, & в описании/параметрах), весь скрипт падал
 * и табы переставали переключаться. Поэтому вынесли сюда,
 * в заведомо валидный main.js.
 */
function initProductTabs() {
    const tabBtns = document.querySelectorAll('.tab-btn');
    const tabPanels = document.querySelectorAll('.tab-panel');

    if (!tabBtns.length || !tabPanels.length) {
        return; // На других страницах табов нет — тихо выходим
    }

    function activateTab(tabId) {
        tabBtns.forEach(function (b) {
            b.classList.toggle('active', b.dataset.tab === tabId);
        });
        tabPanels.forEach(function (p) {
            p.classList.toggle('active', p.id === tabId);
        });
    }

    tabBtns.forEach(function (btn) {
        btn.addEventListener('click', function (e) {
            e.preventDefault();
            const tabId = this.dataset.tab;
            if (!tabId) return;
            activateTab(tabId);
        });
    });

    // Если страница открыта с #reviews — сразу переключаемся
    if (window.location.hash === '#reviews') {
        activateTab('reviews');
    }
}


/**
 * Уведомления
 */
function showNotification(message, type = 'info') {
    // Создание элемента уведомления
    const notification = document.createElement('div');
    notification.className = `flash-message ${type}`;
    notification.style.cssText = `
        position: fixed;
        top: 20px;
        right: 20px;
        padding: 15px 25px;
        border-radius: 8px;
        color: white;
        font-weight: 500;
        z-index: 1000;
        animation: slideIn 0.3s ease;
        background: ${type === 'success' ? '#22c55e' : type === 'error' ? '#ef4444' : '#2563eb'};
    `;
    notification.textContent = message;
    
    document.body.appendChild(notification);
    
    // Автоматическое скрытие
    setTimeout(() => {
        notification.style.opacity = '0';
        notification.style.transform = 'translateX(100%)';
        setTimeout(() => notification.remove(), 300);
    }, 3000);
}


/**
 * Маска для телефона
 */
function initPhoneMask() {
    const phoneInputs = document.querySelectorAll('input[type="tel"]');
    
    phoneInputs.forEach(input => {
        input.addEventListener('input', function(e) {
            let value = e.target.value.replace(/\D/g, '');
            
            if (value.length > 0) {
                if (value[0] === '7' || value[0] === '8') {
                    value = value.substring(1);
                }
                
                let formatted = '+7';
                if (value.length > 0) {
                    formatted += ' (' + value.substring(0, 3);
                }
                if (value.length > 3) {
                    formatted += ') ' + value.substring(3, 6);
                }
                if (value.length > 6) {
                    formatted += '-' + value.substring(6, 8);
                }
                if (value.length > 8) {
                    formatted += '-' + value.substring(8, 10);
                }
                
                e.target.value = formatted;
            }
        });
    });
}


/**
 * AJAX запросы
 */
async function apiRequest(url, options = {}) {
    const defaultOptions = {
        headers: {
            'Content-Type': 'application/x-www-form-urlencoded',
        },
    };
    
    const mergedOptions = { ...defaultOptions, ...options };
    
    try {
        const response = await fetch(url, mergedOptions);
        return await response.json();
    } catch (error) {
        console.error('API Error:', error);
        return { error: 'Произошла ошибка при выполнении запроса' };
    }
}
