'use strict';
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync('static/app.js', 'utf8');
let modal;
class Node {
  constructor(tag) { this.tag = tag; this.children = []; this.listeners = {}; this.style = {}; }
  append(...nodes) { this.children.push(...nodes); }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  showModal() { modal = this; }
  close() { this.listeners.close(); }
  remove() { this.removed = true; }
  focus() { this.focused = true; }
  reportValidity() { return this.children.find(n => n.tag === 'input').value !== ''; }
}
const context = {el: (tag) => new Node(tag), taskLabel: x => x, document: {body: new Node('body')}};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('  function chooseWaitTime('), source.indexOf('  async function openScheduleSettings(')), context);
(async () => {
  let pending = context.chooseWaitTime('DailyTrifles', '2026-10-13 09:00:00');
  let form = modal.children[0], input = form.children[3];
  assert.equal(input.type, 'datetime-local'); assert.equal(input.value, '2026-10-13T09:00:00'); assert(input.focused);
  input.value = ''; form.listeners.submit({preventDefault(){}}); assert(!modal.removed);
  input.value = '2026-10-14T08:15'; form.listeners.submit({preventDefault(){}});
  assert.equal(await pending, '2026-10-14 08:15:00'); assert(modal.removed);
  pending = context.chooseWaitTime('DailyTrifles', '2026-10-13 09:00:00');
  modal.children[0].children[4].children[0].listeners.click(); assert.equal(await pending, null);
  pending = context.chooseWaitTime('DailyTrifles', '2026-10-13 09:00:00'); modal.close(); assert.equal(await pending, null);
  console.log('PASS: wait time picker prefill, selection, validation, cancel and dismissal.');
})().catch(e => {console.error(e); process.exitCode=1;});
