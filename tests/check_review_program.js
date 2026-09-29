const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
    constructor(tag = 'div') { this.tag = tag; this.children = []; this.dataset = {}; this.style = {}; this.events = {}; this.value = ''; }
    append(child) { child.parent = this; this.children.push(child); }
    add(option) { this.append(option); }
    addEventListener(name, callback) { (this.events[name] ||= []).push(callback); }
    fire(name, event = {}) { (this.events[name] || []).forEach(callback => callback(event)); }
    replaceChildren() { this.children = []; }
    get lastElementChild() { return this.children.at(-1); }
    remove() { this.parent.children.splice(this.parent.children.indexOf(this), 1); }
    querySelectorAll() { return this.children.flatMap(child => ['input', 'select'].includes(child.tag) ? [child] : child.querySelectorAll()); }
    checkValidity() { return this.type !== 'number' || this.value === '' || (Number(this.value) >= Number(this.min) && Number(this.value) <= Number(this.max)); }
}
const elements = {};
for (const name of ['review-program-form', 'review-program-data', 'review-kind', 'review-mode', 'review-rules', 'review-rule-error', 'review-rules-json']) elements[name] = new Element();
elements['review-program-data'].textContent = JSON.stringify({rules: [], gifts: [{id: 5, name: 'Gift'}]});
elements['review-kind'].value = 'bonus'; elements['review-mode'].value = 'grades';
let ready;
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../app/static/js/review-program.js'), 'utf8'), {
    document: {addEventListener: (_, fn) => {ready = fn;}, getElementById: id => elements[id], createElement: tag => new Element(tag)},
    Option: class extends Element { constructor(text, value) { super('option'); this.textContent = text; this.value = value; } }
});
ready();
const rows = elements['review-rules'];
function fill(index, threshold, amount) {
    const row = rows.children[index];
    const [number, reward] = row.querySelectorAll();
    number.value = threshold; number.fire('input'); reward.value = amount; row.fire('input');
}
assert.equal(rows.children.length, 1);
assert.equal(rows.children[0].children[1].hidden, true);
fill(0, '1', '50');
assert.equal(rows.children[0].children[1].hidden, false);
assert.equal(rows.children.length, 2);
fill(1, '5', '200');
assert.equal(rows.children.length, 3);
let stopped = false;
elements['review-program-form'].fire('submit', {preventDefault() { stopped = true; }});
assert.equal(stopped, false);
assert.deepEqual(JSON.parse(elements['review-rules-json'].value), [{threshold:'1', amount:'50'}, {threshold:'5', amount:'200'}]);
fill(2, '5', '100');
elements['review-program-form'].fire('submit', {preventDefault() { stopped = true; }});
assert.equal(stopped, true);
elements['review-kind'].value = 'gift'; elements['review-kind'].fire('change');
elements['review-mode'].value = 'each'; elements['review-mode'].fire('change');
fill(0, '1', 'random');
stopped = false;
elements['review-program-form'].fire('submit', {preventDefault() { stopped = true; }});
assert.equal(stopped, false);
assert.equal(rows.children.length, 1);
assert.deepEqual(JSON.parse(elements['review-rules-json'].value), [{threshold:'1', gift:'random'}]);
elements['review-kind'].value = 'discount'; elements['review-kind'].fire('change');
fill(0, '1', '101');
elements['review-program-form'].fire('submit', {preventDefault() { stopped = true; }});
assert.equal(stopped, true);
console.log('PASS: progressive rows, threshold fields, serialization, duplicate prevention, random gift, discount bounds');
