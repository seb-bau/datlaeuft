const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../app/static/js/invoice_cover_letter.js"), "utf8");

class Element {
  constructor() { this.listeners = {}; this.dataset = {}; this.disabled = false; this.hidden = true; this.value = ""; }
  addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler); }
  removeAttribute(name) { delete this[name]; }
  reportValidity() { return true; }
}

function response(data = {}, ok = true, type = "application/json") {
  return {ok, redirected: false, headers: {get: () => type}, json: async () => data, blob: async () => "PDF"};
}

function fixture(fetchResult) {
  const names = ["invoiceCoverLetterModal", "invoiceCoverLetterForm", "coverLetterPreview", "coverLetterPreviewHint", "coverLetterMessage", "coverLetterRemove", "coverLetterPreviewButton", "coverLetterSave", "invoiceCorrespondenceLink", "invoiceCoverLetterTitle"];
  const elements = Object.fromEntries(names.map(name => [name, new Element()]));
  elements.invoiceCoverLetterModal.dataset = {url: "/letter", previewUrl: "/preview"};
  elements.invoiceCoverLetterForm.elements = {title: new Element(), text: new Element(), csrf_token: {value: "test-token"}};
  const calls = [];
  const revoked = [];
  let objectUrls = 0;
  vm.runInNewContext(source, {
    document: {getElementById: name => elements[name], addEventListener: (name, handler) => handler()},
    window: {confirm: () => true},
    FormData: class extends Map { constructor(form) { super(Object.entries(form.elements).map(([key, value]) => [key, value.value])); } },
    URL: {createObjectURL: () => "blob:" + ++objectUrls, revokeObjectURL: value => revoked.push(value)},
    fetch: async (url, options = {}) => {
      calls.push({url, ...options});
      const result = fetchResult && await fetchResult(url, options);
      if (result) return result;
      if (url === "/preview") return response({}, true, "application/pdf");
      if (!options.method) return response({title: "Mieterbelastung {invoice_number}", text: "Guten Tag", saved: true});
      return response({message: "Gespeichert", url: "/correspondence"});
    }
  });
  const fire = async (name, event) => {
    for (const handler of elements[name].listeners[event] || []) await handler({preventDefault() {}});
  };
  return {elements, calls, revoked, fire};
}

const settle = () => new Promise(resolve => setImmediate(resolve));

test("the overlay loads saved text and disables saving after a failed lookup", async () => {
  let failed = true;
  const state = fixture((url, options) => !options.method && failed ? response({message: "Nicht angemeldet"}, false) : null);
  await state.fire("invoiceCoverLetterModal", "show.bs.modal");
  assert.equal(state.elements.coverLetterSave.disabled, true);
  assert.equal(state.elements.coverLetterMessage.textContent, "Nicht angemeldet");
  await state.fire("coverLetterPreviewButton", "click");
  assert.equal(state.calls.length, 1);
  failed = false;
  await state.fire("invoiceCoverLetterModal", "show.bs.modal");
  assert.equal(state.elements.invoiceCoverLetterForm.elements.title.value, "Mieterbelastung {invoice_number}");
  assert.equal(state.elements.coverLetterSave.disabled, false);
});

test("editing invalidates the PDF preview and frees its object URL", async () => {
  const state = fixture();
  await state.fire("invoiceCoverLetterModal", "show.bs.modal");
  await state.fire("coverLetterPreviewButton", "click");
  assert.equal(state.elements.coverLetterPreview.hidden, false);
  assert.equal(state.elements.coverLetterPreview.src, "blob:1");
  state.elements.invoiceCoverLetterForm.elements.text.value = "Neuer Text";
  await state.fire("invoiceCoverLetterForm", "input");
  assert.equal(state.elements.coverLetterPreview.hidden, true);
  assert.deepEqual(state.revoked, ["blob:1"]);
});

test("late previews cannot populate a closed overlay", async () => {
  let resolve;
  const state = fixture(url => url === "/preview" ? new Promise(done => { resolve = done; }) : null);
  await state.fire("invoiceCoverLetterModal", "show.bs.modal");
  const preview = state.fire("coverLetterPreviewButton", "click");
  await settle();
  await state.fire("invoiceCoverLetterModal", "hidden.bs.modal");
  resolve(response({}, true, "application/pdf"));
  await preview;
  assert.equal(state.elements.coverLetterPreview.hidden, true);
  assert.equal(state.elements.coverLetterPreview.src, undefined);
});

test("a save blocks duplicate requests and submits only the letter form", async () => {
  let resolve;
  const state = fixture((url, options) => options.method === "POST" ? new Promise(done => { resolve = done; }) : null);
  await state.fire("invoiceCoverLetterModal", "show.bs.modal");
  state.elements.invoiceCoverLetterForm.elements.text.value = "Individuelles Anschreiben";
  await state.fire("invoiceCoverLetterForm", "submit");
  await settle();
  await state.fire("invoiceCoverLetterForm", "submit");
  const requests = state.calls.filter(call => call.method === "POST");
  assert.equal(requests.length, 1);
  assert.equal(requests[0].body.get("text"), "Individuelles Anschreiben");
  assert.equal(requests[0].body.get("csrf_token"), "test-token");
  assert.equal(state.elements.coverLetterSave.disabled, true);
  resolve(response({message: "Gespeichert"}));
  await settle();
  assert.equal(state.elements.coverLetterSave.disabled, false);
  assert.equal(state.elements.invoiceCorrespondenceLink.hidden, false);
});

test("removing a letter sends its CSRF token and hides the correspondence link", async () => {
  const state = fixture();
  await state.fire("invoiceCoverLetterModal", "show.bs.modal");
  await state.fire("coverLetterRemove", "click");
  await settle();
  const request = state.calls.find(call => call.method === "DELETE");
  assert.equal(request.headers["X-CSRFToken"], "test-token");
  assert.equal(state.elements.invoiceCorrespondenceLink.hidden, true);
  assert.equal(state.elements.coverLetterRemove.hidden, true);
});

test("an HTML login response is never displayed as a PDF", async () => {
  const state = fixture(url => url === "/preview" ? response({}, true, "text/html") : null);
  await state.fire("invoiceCoverLetterModal", "show.bs.modal");
  await state.fire("coverLetterPreviewButton", "click");
  assert.equal(state.elements.coverLetterPreview.hidden, true);
  assert.match(state.elements.coverLetterMessage.textContent, /konnte nicht erstellt/);
});
