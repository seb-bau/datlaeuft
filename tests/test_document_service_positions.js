const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../app/static/js/document_service_positions.js"), "utf8");
const items = [
  {id: 11, craft_activity_id: 1, code: "I-1", title: "Installation prüfen"},
  {id: 12, craft_activity_id: 1, code: "I-2", title: "Armatur austauschen"},
  {id: 21, craft_activity_id: 2, code: "S-1", title: "Tür reparieren"}
];

class Element {
  constructor() { this.value = ""; this.children = []; this.listeners = {}; this.dataset = {}; this.disabled = false; this.hidden = false; this.events = []; }
  addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
  dispatchEvent(event) { this.events.push(event); (this.listeners[event.type] || []).forEach(handler => handler(event)); }
  replaceChildren(...children) { this.children = children; this.value = ""; }
  add(option) { this.children.push(option); }
  focus() { this.focused = true; }
  click() { if (!this.disabled) this.dispatchEvent({type: "click"}); }
}

function fixture(documentType = "invoice", existingIds = []) {
  const elements = Object.fromEntries(["documentServicePositionModal", "documentServicePositionData", "servicePositionCraft", "servicePositionItem", "servicePositionAdd", "servicePositionStatus", "servicePositionError", "craftActivity", documentType === "offer" ? "offerPositions" : "invoicePositions"].map(id => [id, new Element()]));
  const positions = elements.offerPositions || elements.invoicePositions;
  const ids = [...existingIds];
  positions.querySelectorAll = () => ids.map(id => ({value: String(id)}));
  elements.documentServicePositionModal.dataset.documentType = documentType;
  elements.documentServicePositionData.textContent = JSON.stringify(items);
  let mounted;
  let closed = 0;
  const added = [];
  vm.runInNewContext(source, {
    document: {getElementById: id => elements[id] || null, addEventListener: (type, handler) => { mounted = handler; }},
    Option: class { constructor(text, value) { this.textContent = text; this.value = value; } },
    Event: class { constructor(type, options = {}) { this.type = type; this.bubbles = options.bubbles; } },
    bootstrap: {Modal: {getOrCreateInstance: () => ({hide: () => { closed += 1; }})}},
    createPositionRow: values => { added.push(values); ids.push(values.taskTemplateId); positions.lastElementChild = new Element(); }
  });
  mounted();
  const selectCraft = value => { elements.servicePositionCraft.value = value; elements.servicePositionCraft.dispatchEvent({type: "change"}); };
  const selectPosition = value => { elements.servicePositionItem.value = value; elements.servicePositionItem.dispatchEvent({type: "change"}); };
  return {elements, positions, added, selectCraft, selectPosition, closed: () => closed};
}

test("without a craft no service positions are shown or transferable", () => {
  const state = fixture();
  assert.deepEqual(state.elements.servicePositionItem.children.map(item => item.value), [""]);
  assert.equal(state.elements.servicePositionItem.disabled, true);
  assert.equal(state.elements.servicePositionAdd.disabled, true);
  assert.match(state.elements.servicePositionStatus.textContent, /zuerst ein Gewerk/);
  state.elements.servicePositionAdd.click();
  assert.equal(state.added.length, 0);
});

test("selecting a craft shows only that craft's positions", () => {
  const state = fixture();
  state.selectCraft("1");
  assert.deepEqual(state.elements.servicePositionItem.children.map(item => item.value), ["", "11", "12"]);
  assert.equal(state.elements.servicePositionItem.disabled, false);
  state.selectCraft("2");
  assert.deepEqual(state.elements.servicePositionItem.children.map(item => item.value), ["", "21"]);
});

test("changing or clearing the craft clears a previous position selection", () => {
  const state = fixture();
  state.selectCraft("1");
  state.selectPosition("11");
  assert.equal(state.elements.servicePositionAdd.disabled, false);
  state.selectCraft("2");
  assert.equal(state.elements.servicePositionAdd.disabled, true);
  assert.equal(state.elements.servicePositionItem.value, "");
  state.selectCraft("");
  assert.deepEqual(state.elements.servicePositionItem.children.map(item => item.value), [""]);
});

test("a craft without service positions displays an empty state", () => {
  const state = fixture();
  state.selectCraft("999");
  assert.equal(state.elements.servicePositionItem.disabled, true);
  assert.match(state.elements.servicePositionStatus.textContent, /keine Leistungspositionen/);
});

test("a position from another craft cannot be submitted", () => {
  const state = fixture();
  state.selectCraft("1");
  state.selectPosition("21");
  assert.equal(state.elements.servicePositionAdd.disabled, true);
  state.elements.servicePositionAdd.dispatchEvent({type: "click"});
  assert.equal(state.added.length, 0);
});

test("confirming transfers the selected position into the document and closes the overlay", () => {
  for (const type of ["invoice", "offer"]) {
    const state = fixture(type);
    state.selectCraft("1");
    state.selectPosition("12");
    state.elements.servicePositionAdd.click();
    assert.equal(state.added.length, 1);
    assert.equal(state.added[0].sourceType, "catalog");
    assert.equal(state.added[0].taskTemplateId, 12);
    assert.equal(state.added[0].description, "Armatur austauschen");
    assert.equal(state.positions.lastElementChild.events[0].type, "input");
    assert.equal(state.positions.lastElementChild.events[0].bubbles, true);
    assert.equal(state.closed(), 1);
    if (type === "offer") assert.equal(state.elements.craftActivity.value, "1");
  }
});

test("reopening the overlay starts with an explicit craft selection", () => {
  const state = fixture();
  state.selectCraft("1");
  state.selectPosition("11");
  state.elements.documentServicePositionModal.dispatchEvent({type: "show.bs.modal"});
  assert.equal(state.elements.servicePositionCraft.value, "");
  assert.deepEqual(state.elements.servicePositionItem.children.map(item => item.value), [""]);
  assert.equal(state.elements.servicePositionAdd.disabled, true);
});

test("an offer retains its single-craft rule while invoices may mix crafts", () => {
  const offer = fixture("offer", [11]);
  offer.selectCraft("2");
  offer.selectPosition("21");
  offer.elements.servicePositionAdd.click();
  assert.equal(offer.added.length, 0);
  assert.equal(offer.elements.servicePositionError.hidden, false);
  assert.match(offer.elements.servicePositionError.textContent, /anderen Gewerk/);
  const invoice = fixture("invoice", [11]);
  invoice.selectCraft("2");
  invoice.selectPosition("21");
  invoice.elements.servicePositionAdd.click();
  assert.equal(invoice.added.length, 1);
});
