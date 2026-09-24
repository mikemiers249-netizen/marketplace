document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-stock-url]').forEach(cell => {
        let editing = false;
        let saving = false;
        function display(message = '') {
            editing = false;
            const quantity = Number(cell.dataset.quantity);
            const value = document.createElement('span');
            value.className = quantity > 0 ? 'in-stock' : 'out-of-stock';
            value.textContent = quantity > 0 ? `${quantity} шт.` : 'Нет в наличии';
            cell.replaceChildren(value);
            if (message) {
                const error = document.createElement('small');
                error.setAttribute('role', 'alert');
                error.style.display = 'block';
                error.textContent = message;
                cell.append(error);
            }
        }
        function edit() {
            if (editing || saving) return;
            editing = true;
            const input = document.createElement('input');
            input.type = 'number';
            input.min = '0';
            input.max = '2147483647';
            input.step = '1';
            input.value = cell.dataset.quantity;
            input.className = 'form-input';
            input.style.width = '110px';
            input.setAttribute('aria-label', 'Количество на складе');
            cell.replaceChildren(input);
            input.addEventListener('keydown', event => {
                if (event.key === 'Escape') {
                    event.preventDefault();
                    editing = false;
                    display();
                    cell.focus();
                } else if (event.key === 'Enter') {
                    event.preventDefault();
                    input.blur();
                }
            });
            input.addEventListener('blur', async () => {
                if (!editing || saving) return;
                const quantity = Number(input.value);
                if (!input.value.trim() || !Number.isInteger(quantity)
                    || quantity < 0 || quantity > 2147483647) {
                    display('Не сохранено: введите целое неотрицательное количество.');
                    return;
                }
                if (quantity === Number(cell.dataset.quantity)) {
                    display();
                    return;
                }
                saving = true;
                input.disabled = true;
                cell.setAttribute('aria-busy', 'true');
                try {
                    const response = await fetch(cell.dataset.stockUrl, {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'X-CSRFToken': document.querySelector('meta[name="csrf-token"]').content
                        },
                        body: JSON.stringify({stock_quantity: quantity})
                    });
                    if (!response.ok || response.redirected) throw new Error('Save failed');
                    const data = await response.json();
                    if (!Number.isInteger(data.stock_quantity)) throw new Error('Invalid response');
                    cell.dataset.quantity = String(data.stock_quantity);
                    display();
                } catch (error) {
                    display('Не удалось сохранить. Повторите двойной клик и попробуйте ещё раз.');
                } finally {
                    saving = false;
                    cell.removeAttribute('aria-busy');
                }
            });
            input.focus();
            input.select();
        }
        cell.addEventListener('dblclick', edit);
        cell.addEventListener('keydown', event => {
            if (event.target === cell && event.key === 'Enter') {
                event.preventDefault();
                edit();
            }
        });
    });
});
