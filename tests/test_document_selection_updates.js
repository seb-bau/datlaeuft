const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

class Element {
  constructor() { this.value = ""; this.dataset = {}; this.listeners = {}; this.children = []; this.hidden = false; this.classList = {add() {}, remove() {}, toggle() {}}; this.events = []; }
  addEventListener(type, handler) { this.listeners[type] = handler; }
  dispatchEvent(event) { this.events.push(event); return this.listeners[event.type]?.(event); }
  appendChild(child) { this.children.push(child); }
  replaceChildren(...children) { this.children = children; }
  add(option) { this.children.push(option); }
  insertRow() { const row = new Element(); row.insertCell = () => { const cell = new Element(); row.children.push(cell); return cell; }; this.children.push(row); return row; }
  querySelectorAll() { return []; }
  focus() {}
  reset() {}
}

const settle = () => new Promise(resolve => setImmediate(resolve));
const response = data => ({ok: true, redirected: false, json: async () => data});
const Option = class { constructor(text, value) { this.textContent = text; this.value = value; } };
const Event = class { constructor(type, values) { this.type = type; Object.assign(this, values); } };

test("invoice and offer tenant results include contract, apartment address and location", async () => {
  const source = fs.readFileSync(path.join(__dirname, "../app/static/js/recipient_lookup.js"), "utf8");
  for (const kind of ["invoice", "offer"]) {
    const elements = {};
    const element = id => elements[id] ||= new Element();
    const search = element("tenantSearch");
    const form = new Element();
    search.id = "tenantSearch";
    search.dataset = {documentType: kind, searchUrl: "/search", recipientUrl: "/recipient"};
    search.closest = () => form;
    const row = {object_type: "license_agreement", object_id: 4, tenant_name: "Testperson", license_agreement: {erp_id: 44, id_num: "NV-123"},
      economic_unit: {id_num: "WE-1"}, building: {address: "Gebäudeadresse 1"}, use_unit: {address: "Wohnungsstraße 2", floor: "2. OG", position: "links"}};
    vm.runInNewContext(source, {
      document: {addEventListener: (type, handler) => handler(), querySelectorAll: () => [search], getElementById: element, createElement: () => new Element()},
      window: {location: {origin: "https://example.invalid"}}, URL, AbortController, Option, Event, CustomEvent: Event,
      setTimeout: () => 1, clearTimeout() {},
      fetch: async url => response({items: new URL(url).pathname === "/search" ? [row] : [{name: "Testperson"}]})
    });
    search.value = "Test";
    search.listeners.keydown({key: "Enter", preventDefault() {}});
    await settle();
    const button = element("tenantSearchResults").children[0];
    for (const text of ["NV-123", "Wohnungsstraße 2", "2. OG", "links"]) assert.ok(button.textContent.includes(text), text);
    button.listeners.click();
    await settle();
    const event = form.events.find(item => item.type === "invoice-agreement-selected");
    if (kind === "invoice") assert.equal(event.detail.agreementId, "44");
    else assert.equal(event, undefined);
  }
});

test("article dialogs start with both sources and new articles have an explicit target source", async () => {
  const source = fs.readFileSync(path.join(__dirname, "../app/static/js/document_articles.js"), "utf8");
  const elements = {};
  const element = id => id === "offerPositions" ? null : elements[id] ||= new Element();
  element("documentArticleModal").dataset.endpoint = "/items";
  const calls = [];
  vm.runInNewContext(source, {
    document: {getElementById: element, addEventListener: (type, handler) => handler(), createElement: () => new Element()},
    window: {location: {origin: "https://example.invalid"}}, URL, AbortController, Option, Event, FormData: class {},
    setTimeout: () => 1, clearTimeout() {}, createPositionRow() {}, bootstrap: {Modal: {getOrCreateInstance: () => ({hide() {}})}},
    fetch: async (url, options) => { calls.push({url: new URL(url), options}); return response(options.method === "POST" ? {item: {active: false}} : {items: [], item_types: [], has_more: false}); }
  });
  element("documentArticleModal").listeners["show.bs.modal"]();
  await settle();
  assert.equal(calls[0].url.searchParams.get("source"), "all");
  assert.equal(element("documentArticleSource").value, "all");
  element("documentArticleNew").listeners.click();
  assert.equal(element("newArticleSource").value, "local_catalog");
  await element("documentArticleForm").listeners.submit({preventDefault() {}});
  assert.equal(calls.at(-1).url.searchParams.get("source"), "local_catalog");
  element("newArticleSource").value = "material";
  element("newArticleSource").listeners.change();
  await element("documentArticleForm").listeners.submit({preventDefault() {}});
  assert.equal(calls.at(-1).url.searchParams.get("source"), "material");
  element("documentArticleSource").value = "local_catalog";
  element("documentArticleModal").listeners["show.bs.modal"]();
  await settle();
  assert.equal(calls.at(-1).url.searchParams.get("source"), "all");
});
