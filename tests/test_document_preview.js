const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../app/static/js/document_preview.js"), "utf8");

class Element {
  constructor() { this.listeners = {}; this.dataset = {}; this.hidden = false; }
  addEventListener(type, handler) { this.listeners[type] = handler; }
  removeAttribute(name) { delete this[name]; }
}

function fixture({withForm = true, valid = true, fetchResponse} = {}) {
  const elements = Object.fromEntries(["documentPreviewModal", "documentPreviewFrame", "documentPreviewLoading", "documentPreviewError", "documentPreviewOpen", "documentPreviewTitle"].map(id => [id, new Element()]));
  const button = new Element();
  button.dataset = {previewUrl: "/preview", previewTitle: "Rechnung · PDF-Vorschau"};
  const form = {reportValidity: () => valid};
  if (withForm) button.dataset.previewForm = "draft";
  elements.draft = form;
  const calls = [];
  const revoked = [];
  const handlers = {};
  let shown = 0;
  class File {}
  class FormData {
    constructor() { this.values = new Map([["csrf_token", "token"], ["position_article_number", "FREI-1"], ["photo", new File()], ["other_photo", new File()]]); }
    entries() { return this.values.entries(); }
    delete(key) { this.values.delete(key); }
    get(key) { return this.values.get(key); }
  }
  vm.runInNewContext(source, {
    document: {getElementById: id => elements[id], querySelectorAll: () => [button], addEventListener: (type, handler) => { handlers[type] = handler; }},
    window: {addEventListener: (type, handler) => { handlers[type] = handler; }},
    bootstrap: {Modal: {getOrCreateInstance: () => ({show: () => { shown += 1; }})}},
    fetch: async (url, options) => { calls.push({url, ...options}); return fetchResponse ? fetchResponse(url, options) : pdf(); },
    URL: {createObjectURL: () => "blob:preview", revokeObjectURL: url => revoked.push(url)},
    AbortController, FormData, File
  });
  handlers.DOMContentLoaded();
  return {elements, calls, revoked, handlers, shown: () => shown, click: () => button.listeners.click()};
}

function pdf() {
  return {ok: true, redirected: false, headers: {get: () => "application/pdf"}, blob: async () => ({size: 100})};
}

test("preview sends current fields and CSRF without submitting or uploading files", async () => {
  const state = fixture();
  await state.click();
  assert.equal(state.shown(), 1);
  assert.equal(state.calls[0].method, "POST");
  assert.equal(state.calls[0].body.get("position_article_number"), "FREI-1");
  assert.equal(state.calls[0].body.get("csrf_token"), "token");
  assert.equal(state.calls[0].body.get("photo"), undefined);
  assert.equal(state.calls[0].body.get("other_photo"), undefined);
  assert.equal(state.elements.documentPreviewFrame.src, "blob:preview");
  assert.equal(state.elements.documentPreviewOpen.href, "blob:preview");
  assert.equal(state.elements.documentPreviewLoading.hidden, true);
});

test("invalid input stays in the form", async () => {
  const state = fixture({valid: false});
  await state.click();
  assert.equal(state.calls.length, 0);
  assert.equal(state.shown(), 0);
});

test("stored orders are requested without a form body", async () => {
  const state = fixture({withForm: false});
  await state.click();
  assert.equal(state.calls[0].method, "GET");
  assert.equal(state.calls[0].body, undefined);
});

test("validation errors and expired sessions are visible instead of embedding HTML", async () => {
  for (const [response, message] of [
    [{ok: false, headers: {get: () => "application/json"}, json: async () => ({error: "Die Artikelnummer ist zu lang."})}, /Artikelnummer ist zu lang/],
    [{ok: true, redirected: true, headers: {get: () => "text/html"}}, /Anmeldung prüfen/]
  ]) {
    const state = fixture({fetchResponse: async () => response});
    await state.click();
    assert.equal(state.elements.documentPreviewError.hidden, false);
    assert.match(state.elements.documentPreviewError.textContent, message);
    assert.equal(state.elements.documentPreviewFrame.src, undefined);
  }
});

test("closing the preview aborts requests and releases document URLs", async () => {
  const state = fixture();
  await state.click();
  state.elements.documentPreviewModal.listeners["hidden.bs.modal"]();
  assert.equal(state.calls[0].signal.aborted, true);
  assert.deepEqual(state.revoked, ["blob:preview"]);
  assert.equal(state.elements.documentPreviewFrame.src, undefined);
});

test("late results from a closed preview do not reopen its document", async () => {
  let resolve;
  const state = fixture({fetchResponse: () => new Promise(done => { resolve = done; })});
  const pending = state.click();
  state.elements.documentPreviewModal.listeners["hidden.bs.modal"]();
  resolve(pdf());
  await pending;
  assert.equal(state.elements.documentPreviewFrame.src, undefined);
  assert.equal(state.elements.documentPreviewError.hidden, true);
});
