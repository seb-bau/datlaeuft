const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../app/static/js/app.js"), "utf8");

class Element {
  constructor() { this.dataset = {}; this.children = []; this.listeners = {}; this.textContent = ""; }
  appendChild(child) { this.children.push(child); }
  setAttribute(name, value) { this[name] = value; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
}

function storage(values = new Map()) {
  return {get length() { return values.size; }, key: index => [...values.keys()][index],
    getItem: key => values.get(key) || null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key), values};
}

function fixture({saved = storage(), search = "?creator=all&craftsman=user%3A1", stateKey = "commission-list-1", optIn = true} = {}) {
  const table = new Element();
  if (optIn) table.dataset.stateKey = stateKey;
  const head = new Element();
  const titles = ["Nummer", "Titel", "Bearbeitungsstand", ""].map(text => Object.assign(new Element(), {textContent: text}));
  head.rows = [{cells: titles}];
  table.tHead = head;
  const reset = new Element();
  reset.dataset.resetTableState = stateKey;
  let instance;
  vm.runInNewContext(source, {
    document: {addEventListener: (name, handler) => handler(), createElement: () => new Element(),
      querySelectorAll: selector => selector === "table.datatable" ? [table] : [reset]},
    window: {location: {pathname: "/admin/commissions", search}, sessionStorage: saved},
    DataTable: class {
      constructor(element, options) {
        this.options = options;
        this.loaded = options.stateLoadCallback?.() || null;
        this.columns = titles.map((title, index) => {
          let value = this.loaded?.columns?.[index]?.search?.search || "";
          return {search(next) { if (next === undefined) return value; value = next; return this; }, draw() { return this; }};
        });
        instance = this;
      }
      column(index) { return this.columns[index]; }
    }
  });
  const inputs = head.children[0].children.filter(cell => cell.children.length).map(cell => cell.children[0]);
  return {instance, reset, inputs, saved};
}

test("returning to the same filtered list restores searches, columns, ordering and page", () => {
  const first = fixture();
  const snapshot = {time: Date.now(), start: 25, length: 25, order: [[1, "desc"]], search: {search: "Heizung"},
    columns: [{search: {search: "A-"}}, {search: {search: "Ventil"}}, {search: {search: "Warte auf Annahme"}}, {search: {search: ""}}]};
  first.instance.options.stateSaveCallback({}, snapshot);
  const returned = fixture({saved: first.saved});
  assert.equal(returned.instance.options.stateSave, true);
  assert.equal(returned.instance.options.stateDuration, -1);
  assert.equal(returned.instance.loaded.start, 25);
  assert.equal(returned.instance.loaded.search.search, "Heizung");
  assert.equal(JSON.stringify(returned.instance.loaded.order), JSON.stringify(snapshot.order));
  assert.deepEqual(returned.inputs.map(input => input.value), ["A-", "Ventil", "Warte auf Annahme"]);
  returned.inputs[0].value = "A-123";
  returned.inputs[0].listeners.input();
  assert.equal(returned.instance.column(0).search(), "A-123");
});

test("different filter selections and users keep independent table states", () => {
  const first = fixture();
  first.instance.options.stateSaveCallback({}, {start: 25});
  assert.equal(fixture({saved: first.saved, search: "?creator=all&craftsman=user%3A2"}).instance.loaded, null);
  assert.equal(fixture({saved: first.saved, stateKey: "commission-list-2"}).instance.loaded, null);
  assert.equal(fixture({saved: storage()}).instance.loaded, null);
});

test("type selections and a new column schema use independent saved table states", () => {
  const first = fixture({search: "?commission_type=100", stateKey: "commission-list-1-v3"});
  first.instance.options.stateSaveCallback({}, {start: 25});
  assert.equal(fixture({saved: first.saved, search: "?commission_type=100", stateKey: "commission-list-1-v3"}).instance.loaded.start, 25);
  assert.equal(fixture({saved: first.saved, search: "?commission_type=200", stateKey: "commission-list-1-v3"}).instance.loaded, null);
  assert.equal(fixture({saved: first.saved, search: "?commission_type=100", stateKey: "commission-list-1-v2"}).instance.loaded, null);
});

test("reset clears this order list's saved states and preserves other users and tables", () => {
  const first = fixture();
  first.instance.options.stateSaveCallback({}, {start: 25});
  fixture({saved: first.saved, search: ""}).instance.options.stateSaveCallback({}, {start: 50});
  fixture({saved: first.saved, stateKey: "commission-list-2"}).instance.options.stateSaveCallback({}, {start: 75});
  first.saved.setItem("another-table", "preserve");
  first.reset.listeners.click();
  assert.equal(fixture({saved: first.saved}).instance.loaded, null);
  assert.equal(fixture({saved: first.saved, search: ""}).instance.loaded, null);
  assert.equal(fixture({saved: first.saved, stateKey: "commission-list-2"}).instance.loaded.start, 75);
  assert.equal(first.saved.getItem("another-table"), "preserve");
});

test("unavailable or malformed storage does not prevent filtering", () => {
  const broken = {getItem() { throw new Error("Storage unavailable"); }, setItem() { throw new Error("Storage unavailable"); }, get length() { throw new Error("Storage unavailable"); }};
  const state = fixture({saved: broken});
  assert.equal(state.instance.loaded, null);
  assert.doesNotThrow(() => state.instance.options.stateSaveCallback({}, {start: 0}));
  assert.doesNotThrow(() => state.reset.listeners.click());
  const malformed = storage();
  malformed.setItem("datatable:commission-list-1:/admin/commissions:?creator=all&craftsman=user%3A1", "{");
  assert.equal(fixture({saved: malformed}).instance.loaded, null);
});

test("other tables retain their existing behavior", () => {
  const state = fixture({optIn: false});
  assert.equal(state.instance.options.stateSave, undefined);
  assert.equal(state.instance.options.stateLoadCallback, undefined);
  assert.equal(state.reset.listeners.click, undefined);
});
