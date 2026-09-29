document.addEventListener('DOMContentLoaded', () => {
    const form = document.getElementById('review-program-form');
    if (!form) return;
    const data = JSON.parse(document.getElementById('review-program-data').textContent);
    const kind = document.getElementById('review-kind');
    const mode = document.getElementById('review-mode');
    const rows = document.getElementById('review-rules');
    const error = document.getElementById('review-rule-error');
    function complete(row) {
        return [...row.querySelectorAll('input, select')].every(el => el.value !== '' && el.checkValidity());
    }
    function addRow(rule = {}) {
        const row = document.createElement('div');
        row.style.cssText = 'display:flex;gap:12px;align-items:end;flex-wrap:wrap;margin:12px 0';
        const thresholdLabel = document.createElement('label');
        thresholdLabel.textContent = mode.value === 'each' ? 'Каждый отзыв' : 'Номер отзыва';
        const threshold = document.createElement('input');
        Object.assign(threshold, {type:'number', min:'1', max:'1000000', step:'1', className:'form-control'});
        threshold.dataset.field = 'threshold';
        threshold.value = mode.value === 'each' ? '1' : (rule.threshold || '');
        threshold.hidden = mode.value === 'each';
        thresholdLabel.append(threshold);
        row.append(thresholdLabel);
        const rewardLabel = document.createElement('label');
        rewardLabel.textContent = kind.value === 'gift' ? 'Подарок' : kind.value === 'discount' ? 'Скидка, %' : 'Баллы, ₽';
        const input = document.createElement(kind.value === 'gift' ? 'select' : 'input');
        input.className = 'form-control';
        input.dataset.field = kind.value === 'gift' ? 'gift' : 'amount';
        if (kind.value === 'gift') {
            input.add(new Option('Выберите подарок', ''));
            input.add(new Option('Случайный из галереи', 'random'));
            data.gifts.forEach(gift => input.add(new Option(gift.name, String(gift.id))));
            input.value = rule.gift || '';
        } else {
            Object.assign(input, {type:'number', min:'0.01', max:kind.value === 'discount' ? '100' : '9999999.99', step:'0.01'});
            input.value = rule.amount || '';
        }
        rewardLabel.append(input);
        rewardLabel.hidden = mode.value === 'grades' && !threshold.value;
        row.append(rewardLabel);
        threshold.addEventListener('input', () => { rewardLabel.hidden = !threshold.value; });
        if (mode.value === 'grades') {
            const remove = document.createElement('button');
            remove.type = 'button'; remove.textContent = 'Удалить'; remove.className = 'btn btn-secondary';
            remove.addEventListener('click', () => { row.remove(); ensureBlank(); });
            row.append(remove);
        }
        row.addEventListener('input', ensureBlank);
        row.addEventListener('change', ensureBlank);
        rows.append(row);
    }
    function ensureBlank() {
        if (mode.value === 'grades' && (!rows.lastElementChild || complete(rows.lastElementChild))) addRow();
    }
    function render(rules) {
        rows.replaceChildren();
        if (mode.value === 'each') addRow(rules[0] || {});
        else { rules.forEach(addRow); ensureBlank(); }
    }
    kind.addEventListener('change', () => render([]));
    mode.addEventListener('change', () => render([]));
    form.addEventListener('submit', event => {
        error.textContent = '';
        const rules = [];
        for (const row of rows.children) {
            const inputs = [...row.querySelectorAll('input, select')];
            if (mode.value === 'grades' && inputs.every(el => !el.value)) continue;
            if (!complete(row)) { error.textContent = 'Заполните начатые строки или удалите их.'; event.preventDefault(); return; }
            rules.push(Object.fromEntries(inputs.map(el => [el.dataset.field, el.value])));
        }
        if (!rules.length || new Set(rules.map(r => Number(r.threshold))).size !== rules.length) {
            error.textContent = 'Добавьте правило. Номера отзывов не должны повторяться.'; event.preventDefault(); return;
        }
        document.getElementById('review-rules-json').value = JSON.stringify(rules);
    });
    render(data.rules);
});
