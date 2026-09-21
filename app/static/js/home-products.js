document.addEventListener('DOMContentLoaded', () => {
    const loader = document.getElementById('home-products-loader');
    if (!loader) return;
    const grid = document.getElementById('home-products-grid');
    const button = loader.querySelector('button');
    const status = loader.querySelector('[role="status"]');
    const seen = new Set(Array.from(grid.querySelectorAll('.product-card'), card => Number(card.dataset.productId)));
    let loading = false;
    let finished = false;
    let failed = false;
    let observer;

    async function loadMore() {
        if (loading || finished) return;
        loading = true;
        failed = false;
        button.disabled = true;
        status.textContent = 'Загрузка товаров…';
        try {
            const response = await fetch(loader.dataset.url, {
                method: 'POST',
                headers: {'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken()},
                body: JSON.stringify({excluded_ids: Array.from(seen)})
            });
            if (!response.ok) throw new Error('Failed to load products');
            const data = await response.json();
            if (!Array.isArray(data.ids) || ![0, 5].includes(data.ids.length)
                || new Set(data.ids).size !== data.ids.length || data.ids.some(id => seen.has(id))) {
                throw new Error('Invalid product batch');
            }
            const template = document.createElement('template');
            template.innerHTML = data.html;
            initFavoriteButtons(template.content);
            grid.append(template.content);
            data.ids.forEach(id => seen.add(id));
            finished = !data.has_more || data.ids.length === 0;
            status.textContent = '';
            if (finished) {
                loader.hidden = true;
                if (observer) observer.disconnect();
            }
        } catch (error) {
            failed = true;
            status.textContent = 'Не удалось загрузить товары. Попробуйте ещё раз.';
        } finally {
            loading = false;
            button.disabled = false;
        }
        if (!finished && !failed && loader.getBoundingClientRect().top <= window.innerHeight + 200) {
            requestAnimationFrame(loadMore);
        }
    }
    button.addEventListener('click', loadMore);
    if ('IntersectionObserver' in window) {
        observer = new IntersectionObserver(entries => {
            if (entries.some(entry => entry.isIntersecting) && !failed) loadMore();
        }, {rootMargin: '200px'});
        observer.observe(loader);
    }
});
