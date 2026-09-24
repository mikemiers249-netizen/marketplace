const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const template = fs.readFileSync(path.join(__dirname, '../app/templates/seller/product_form.html'), 'utf8');
const source = template.slice(template.indexOf('function buildCategoryPath('),
    template.indexOf('// Загрузка параметров для выбранной категории'));
const categories = [
    {id: 1, parent_id: null, name: 'Root'},
    {id: 2, parent_id: 1, name: 'Child'},
    {id: 3, parent_id: 2, name: 'Leaf'},
    {id: 4, parent_id: 1, name: 'Other child'}
];
function element() {
    return {value: '', dataset: {}, style: {}, addEventListener() {}};
}
const root = element();
root.id = 'category_level_0';
const hidden = element();
const selector = {
    children: [root],
    get lastChild() { return this.children.at(-1); },
    appendChild(child) { this.children.push(child); },
    removeChild(child) { this.children.splice(this.children.indexOf(child), 1); }
};
const context = vm.createContext({
    allCategories: categories,
    document: {
        getElementById(id) {
            if (id === 'category-selector') return selector;
            if (id === 'category_id') return hidden;
            if (id === 'parameters-section') return element();
            return selector.children.find(child => child.id === id);
        },
        createElement: element
    },
    loadParametersForCategory() {}, updateCardOptions() {}
});
vm.runInContext(source, context);
for (const [selected, expected] of [[3, [1, 2, 3]], [2, [1, 2, '']], [1, [1, '']], [4, [1, 4]], [3, [1, 2, 3]]]) {
    let callbacks = 0;
    context.buildCategoryPath(selected, () => callbacks++);
    assert.equal(Number(hidden.value), selected, 'submitted category must remain the selected category');
    assert.deepEqual(selector.children.map(child => String(child.value)), expected.map(String));
    assert.equal(new Set(selector.children.map(child => child.id)).size, selector.children.length);
    assert.equal(callbacks, 1);
}
context.handleCategoryChange(1, '4');
assert.equal(hidden.value, '4');
assert.equal(selector.children.length, 2);
context.handleCategoryChange(0, '1');
assert.equal(hidden.value, '1');
assert.equal(selector.children.length, 2);
console.log('PASS: root, nested categories, reopening/copy initialization, unique selects, manual category changes');
