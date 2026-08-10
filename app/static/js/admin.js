/**
 * admin.js — клиентская логика для админ-панели.
 *
 * Изначально файл был утерян, из-за чего в Network отдавался 404
 * на /static/js/admin.js. Сейчас здесь минимум — базовая инициализация
 * и периодический опрос счётчика непрочитанных сообщений, чтобы бейдж
 * в пункте «Сообщения» обновлялся в реальном времени, без перезагрузки.
 */

(function () {
    'use strict';

    document.addEventListener('DOMContentLoaded', function () {
        // CSRF-токен для потенциальных AJAX-запросов
        window.getCsrfToken = function () {
            const meta = document.querySelector('meta[name="csrf-token"]');
            return meta ? meta.getAttribute('content') : '';
        };

        // Кнопки с подтверждением удаления
        document.querySelectorAll('form[data-confirm]').forEach(function (form) {
            form.addEventListener('submit', function (e) {
                const msg = form.getAttribute('data-confirm') || 'Точно удалить?';
                if (!confirm(msg)) {
                    e.preventDefault();
                }
            });
        });

        // Лёгкий live-update счётчика непрочитанных сообщений.
        // Если на странице есть пункт меню «Сообщения» с .unread-dot —
        // опрашиваем сервер раз в 30 секунд и переключаем класс has-unread.
        startUnreadMessagesPolling();
    });

    function startUnreadMessagesPolling() {
        // Этот блок работает только для залогиненных через Flask-Login
        // (покупатель/продавец/Admin). Для main_admin, который авторизуется
        // через session['main_admin_authenticated'], polling бессилен —
        // эндпоинт /api/notifications/count вернёт 401/403, и мы тихо
        // отключаемся. Подсветка для main_admin обновляется через обычный
        // рендер страницы.
        const meta = document.querySelector('meta[name="csrf-token"]');
        if (!meta || !meta.getAttribute('content')) return;

        const messagesLink = document.querySelector(
            '.sidebar-nav a[href*="/main_admin/messages"]'
        );
        if (!messagesLink) return;

        async function refresh() {
            try {
                const resp = await fetch('/api/notifications/count', {
                    credentials: 'same-origin',
                    headers: { 'X-Requested-With': 'XMLHttpRequest' }
                });
                if (!resp.ok) return;
                const data = await resp.json();
                const count = (data && typeof data.unread_messages === 'number')
                    ? data.unread_messages
                    : 0;

                if (count > 0) {
                    messagesLink.classList.add('has-unread');
                    let dot = messagesLink.querySelector('.unread-dot');
                    if (!dot) {
                        dot = document.createElement('span');
                        dot.className = 'unread-dot';
                        dot.title = count + ' непрочитанных сообщений';
                        messagesLink.appendChild(dot);
                    }
                } else {
                    messagesLink.classList.remove('has-unread');
                    const dot = messagesLink.querySelector('.unread-dot');
                    if (dot) dot.remove();
                }
            } catch (err) {
                // Сеть моргнула — не страшно, попробуем через 30 секунд.
            }
        }

        // Первый опрос сразу + затем каждые 30 секунд.
        refresh();
        setInterval(refresh, 30000);
    }
})();
