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
        // опрашиваем сервер раз в 10 секунд и переключаем класс has-unread.
        startUnreadMessagesPolling();
    });

    function startUnreadMessagesPolling() {
        const messagesLink = document.querySelector(
            '[data-unread-url]'
        );
        if (!messagesLink) return;

        let loading = false;
        async function refresh() {
            if (loading || document.hidden) return;
            loading = true;
            try {
                const resp = await fetch(messagesLink.dataset.unreadUrl, {
                    cache: 'no-store',
                    credentials: 'same-origin',
                    headers: { 'X-Requested-With': 'XMLHttpRequest' }
                });
                if (!resp.ok) return;
                const data = await resp.json();
                const count = (data && typeof data.unread_messages === 'number')
                    ? data.unread_messages
                    : 0;

                [messagesLink, ...messagesLink.querySelectorAll('span:not(.unread-dot), i')].forEach(element => {
                    if (count > 0) element.style.setProperty('color', '#ff4d4f', 'important');
                    else element.style.removeProperty('color');
                });
                if (count > 0) {
                    messagesLink.classList.add('has-unread');
                    let dot = messagesLink.querySelector('.unread-dot');
                    if (!dot) {
                        dot = document.createElement('span');
                        dot.className = 'unread-dot';
                        dot.title = count + ' непрочитанных сообщений';
                        messagesLink.appendChild(dot);
                    }
                    dot.title = count + ' непрочитанных сообщений';
                } else {
                    messagesLink.classList.remove('has-unread');
                    const dot = messagesLink.querySelector('.unread-dot');
                    if (dot) dot.remove();
                }
            } catch (err) {
                // При временной сетевой ошибке сохраняем последнюю подсветку.
            } finally {
                loading = false;
            }
        }

        // Первый опрос сразу + затем каждые 10 секунд.
        refresh();
        setInterval(refresh, 10000);
        window.addEventListener('focus', refresh);
        document.addEventListener('visibilitychange', refresh);
    }
})();
