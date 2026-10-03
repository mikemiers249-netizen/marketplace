/* Keep existing forms and AJAX requests protected, including dynamically loaded chats. */
(() => {
    const token = () => document.querySelector('meta[name="csrf-token"]')?.content || '';
    const originalFetch = window.fetch.bind(window);
    window.fetch = (input, init = {}) => {
        const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url, location.href);
        const method = (init.method || (input instanceof Request ? input.method : 'GET')).toUpperCase();
        if (url.origin === location.origin && !['GET', 'HEAD', 'OPTIONS'].includes(method)) {
            const headers = new Headers(init.headers || (input instanceof Request ? input.headers : undefined));
            if (!headers.has('X-CSRFToken')) headers.set('X-CSRFToken', token());
            init = { ...init, headers };
        }
        return originalFetch(input, init);
    };
    const protectForm = form => {
        if ((form.method || 'get').toLowerCase() === 'get' || new URL(form.action, location.href).origin !== location.origin) return;
        if (!form.querySelector('[name="csrf_token"]')) {
            const input = document.createElement('input');
            input.type = 'hidden'; input.name = 'csrf_token'; input.value = token();
            form.appendChild(input);
        }
    };
    document.addEventListener('submit', event => { if (event.target instanceof HTMLFormElement) protectForm(event.target); }, true);
    document.addEventListener('DOMContentLoaded', () => document.querySelectorAll('form').forEach(protectForm));
    const open = XMLHttpRequest.prototype.open;
    const send = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function(method, url, ...rest) {
        this.wimliProtected = !['GET', 'HEAD', 'OPTIONS'].includes(method.toUpperCase()) && new URL(url, location.href).origin === location.origin;
        return open.call(this, method, url, ...rest);
    };
    XMLHttpRequest.prototype.send = function(body) {
        if (this.wimliProtected) this.setRequestHeader('X-CSRFToken', token());
        return send.call(this, body);
    };
    const libraries = new Map();
    window.WimliPrivacy = {
        async externalMap() {
            if (sessionStorage.getItem('wimli-external-map') === 'yes') return true;
            if (!window.confirm('Загрузить внешнюю карту/виджет доставки? Поставщик получит IP-адрес, технические сведения и выбранную область карты. Условия: /page/privacy. Можно отказаться и выбрать ПВЗ из списка или вручную.')) return false;
            sessionStorage.setItem('wimli-external-map', 'yes');
            return true;
        },
        script(url) {
            if (!libraries.has(url)) libraries.set(url, new Promise((resolve, reject) => {
                const script = document.createElement('script'); script.src = url;
                script.onload = resolve; script.onerror = reject; document.head.appendChild(script);
            }));
            return libraries.get(url);
        },
        tileLayer(map, url, options) {
            const container = map.getContainer();
            const button = document.createElement('button');
            button.type = 'button'; button.className = 'btn btn-light';
            button.style.cssText = 'position:absolute;top:10px;right:10px;z-index:1000';
            button.textContent = 'Загрузить внешнюю карту';
            button.addEventListener('click', async event => {
                event.stopPropagation();
                if (await this.externalMap()) { L.tileLayer(url, options).addTo(map); button.remove(); }
            });
            container.appendChild(button);
        }
    };
})();
