const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../app/static/js/invoice_delivery.js"), "utf8");

class Element {
  constructor() { this.listeners = {}; this.children = []; this.dataset = {}; this.value = ""; this.disabled = false; this.hidden = false; this.checked = false; }
  addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler); }
  appendChild(item) { this.children.push(item); }
  replaceChildren(...items) { this.children = items; this.value = ""; }
  add(item) { this.children.push(item); }
  reset() { this.wasReset = true; }
  reportValidity() { return true; }
}

function fixture({editable = true, mail = true, fetchResult} = {}) {
  const names = ["invoiceAttachments", "invoiceAttachmentForm", "invoiceAttachmentList", "invoiceAttachmentMessage", "invoiceAttachmentFiles", "invoiceAttachmentUpload", "invoiceDelivery", "invoiceDeliveryClient", "invoiceDeliverySystem", "invoiceDeliveryOfficial", "invoiceDeliveryPreview", "invoiceDeliverySend", "invoiceDeliveryMessage", "invoiceDeliveryForm", "invoiceDeliveryModes", "invoiceDeliveryPrint", "invoiceDeliveryClientPanel", "invoiceDeliverySystemPanel", "invoiceDeliveryPrintPanel", "invoiceDeliveryRecipient", "invoiceDeliverySubject", "invoiceDeliveryBody", "invoiceDeliveryResetBody", "invoiceDeliveryCsrf"];
  const elements = Object.fromEntries(names.map(name => [name, new Element()]));
  elements.invoiceAttachments.dataset = {endpoint: "/admin/invoices/1/attachments", documentUrl: "/admin/invoices/1/attachments/0"};
  elements.invoiceAttachmentForm.elements = {csrf_token: {value: "csrf-test"}};
  elements.invoiceDeliveryCsrf.value = "csrf-test";
  elements.invoiceDeliveryClient.checked = true;
  elements.invoiceDeliverySend.disabled = true;
  elements.invoiceDeliveryPreview.hidden = true;
  elements.invoiceDelivery.dataset = {recipientsUrl: "/recipients", previewUrl: "/preview", sendUrl: "/send", statusUrl: "/status", invoiceUrl: "/invoice"};
  const navigation = [];
  const calls = [];
  const timers = new Map();
  let nextTimer = 0;
  let nonce = 0;
  const items = [{id: 7, name: "anlage.pdf", pages: 2}];
  vm.runInNewContext(source, {
    document: {
      getElementById: name => name === "invoiceAttachmentForm" && !editable || name === "invoiceDelivery" && !mail ? null : elements[name],
      createElement: () => new Element(), addEventListener: (name, callback) => callback()
    },
    window: {location: {origin: "https://example.invalid", assign: target => navigation.push(target)}}, URL, AbortController,
    FormData: class { constructor(form) { this.form = form; } },
    Option: class { constructor(text, value) { this.textContent = text; this.value = value; } },
    crypto: {randomUUID: () => `request-${++nonce}`},
    setTimeout: callback => { timers.set(++nextTimer, callback); return nextTimer; }, clearTimeout: id => timers.delete(id),
    fetch: async (input, options = {}) => {
      const url = new URL(String(input), "https://example.invalid");
      calls.push({url, ...options});
      if (fetchResult) {
        const result = await fetchResult(url, options);
        if (result) return result;
      }
      if (url.pathname.endsWith("/attachments")) return response({items, editable});
      if (url.pathname === "/recipients") return response({items: [{id: 901, name: "Sachbearbeitung"}, {id: 902, name: "Andere Sachbearbeitung"}]});
      if (url.pathname === "/preview") return response({recipient: "office@example.invalid", subject: "Mieterbelastung - Rechnung RE-1", body: "Bitte bearbeiten."});
      if (url.pathname === "/send") return response({id: 12, state: "pending", recipient: "office@example.invalid"});
      if (url.pathname === "/status") return response({item: null});
      return response({});
    }
  });
  const fire = async (name, event, value = {}) => { for (const handler of elements[name].listeners[event] || []) await handler(value); };
  return {elements, calls, timers, items, fire, navigation,
    openSystem: async () => { elements.invoiceDeliveryPrint.checked = false; elements.invoiceDeliverySystem.checked = true; elements.invoiceDeliveryClient.checked = false; await fire("invoiceDeliverySystem", "change"); },
    choose: async id => { elements.invoiceDeliveryOfficial.value = String(id); await fire("invoiceDeliveryOfficial", "change"); }};
}

function response(data, ok = true) { return {ok, redirected: false, json: async () => data}; }
const settle = () => new Promise(resolve => setImmediate(resolve));

test("attachment names are rendered as text and read-only invoices have no remove buttons", async () => {
  const state = fixture({editable: false, mail: false});
  await settle();
  const row = state.elements.invoiceAttachmentList.children[0];
  assert.equal(row.children.length, 1);
  assert.equal(row.children[0].textContent, "anlage.pdf · 2 Seiten");
  assert.equal(row.children[0].href, "/admin/invoices/1/attachments/7");
});

test("attachment uploads block duplicate submissions and enable the controls again", async () => {
  let resolve;
  const state = fixture({mail: false, fetchResult: async (url, options) => options.method === "POST" ? new Promise(done => { resolve = done; }) : null});
  await settle();
  const uploading = state.fire("invoiceAttachmentForm", "submit", {preventDefault() {}});
  await settle();
  assert.equal(state.elements.invoiceAttachmentUpload.disabled, true);
  assert.equal(state.elements.invoiceAttachmentFiles.disabled, true);
  await state.fire("invoiceAttachmentForm", "submit", {preventDefault() {}});
  assert.equal(state.calls.filter(call => call.method === "POST").length, 1);
  resolve(response({items: state.items, editable: true}));
  await uploading;
  assert.equal(state.elements.invoiceAttachmentUpload.disabled, false);
  assert.equal(state.elements.invoiceAttachmentFiles.disabled, false);
  assert.equal(state.elements.invoiceAttachmentForm.wasReset, true);
});

test("removing an attachment uses the invoice endpoint and the CSRF token", async () => {
  const state = fixture({mail: false});
  await settle();
  const button = state.elements.invoiceAttachmentList.children[0].children[1];
  await button.listeners.click[0]();
  const request = state.calls.find(call => call.method === "DELETE");
  assert.equal(request.url.pathname, "/admin/invoices/1/attachments/7");
  assert.equal(request.headers["X-CSRFToken"], "csrf-test");
});

test("system delivery previews the selected official and reports the completed send", async () => {
  const state = fixture({fetchResult: async url => url.pathname === "/status" && url.searchParams.has("notification_id") ? response({item: {id: 12, state: "sent", recipient: "office@example.invalid"}}) : null});
  await state.openSystem();
  await state.choose(901);
  assert.equal(state.elements.invoiceDeliverySubject.textContent, "Mieterbelastung - Rechnung RE-1");
  assert.equal(state.elements.invoiceDeliveryBody.value, "Bitte bearbeiten.");
  assert.equal(state.elements.invoiceDeliveryPreview.hidden, false);
  await state.fire("invoiceDeliverySend", "click");
  const request = state.calls.find(call => call.url.pathname === "/send");
  assert.equal(JSON.parse(request.body).official_id, "901");
  assert.equal(JSON.parse(request.body).body, "Bitte bearbeiten.");
  assert.equal(request.headers["X-CSRFToken"], "csrf-test");
  assert.equal(JSON.parse(request.body).complete_invoice, true);
  assert.match(state.elements.invoiceDeliveryMessage.textContent, /E-Mail versendet/);
  assert.deepEqual(state.navigation, ["/invoice"]);
  assert.equal(state.elements.invoiceDeliverySend.disabled, true);
});

test("late address responses cannot replace a different official", async () => {
  let resolve;
  const state = fixture({fetchResult: async url => url.pathname === "/preview" && url.searchParams.get("official_id") === "901" ? new Promise(done => { resolve = done; }) : null});
  await state.openSystem();
  const first = state.choose(901);
  await settle();
  await state.choose(902);
  resolve(response({recipient: "wrong@example.invalid", subject: "Falsch", body: "Falsch"}));
  await first;
  assert.equal(state.elements.invoiceDeliveryRecipient.textContent, "office@example.invalid");
});

test("retrying after a lost send response reuses the same request identifier", async () => {
  let attempts = 0;
  const state = fixture({fetchResult: async url => { if (url.pathname === "/send" && attempts++ === 0) throw new Error("Verbindung unterbrochen"); return null; }});
  await state.openSystem();
  await state.choose(901);
  state.elements.invoiceDeliveryBody.value = "Individuelle Hinweise\nZweite Zeile";
  await state.fire("invoiceDeliveryBody", "input");
  await state.fire("invoiceDeliverySend", "click");
  assert.equal(state.elements.invoiceDeliverySend.disabled, false);
  assert.equal(state.elements.invoiceDeliveryBody.readOnly, false);
  await state.fire("invoiceDeliverySend", "click");
  const requests = state.calls.filter(call => call.url.pathname === "/send").map(call => JSON.parse(call.body));
  assert.equal(requests.length, 2);
  assert.equal(requests[0].request_id, requests[1].request_id);
  assert.equal(requests[0].body, "Individuelle Hinweise\nZweite Zeile");
  assert.equal(requests[1].body, requests[0].body);
});

test("individual mail text survives recipient and delivery mode changes and can be reset to the current template", async () => {
  const state = fixture({fetchResult: async url => url.pathname === "/preview" && url.searchParams.get("official_id") === "902"
    ? response({recipient: "other@example.invalid", subject: "Mieterbelastung - Rechnung RE-1", body: "Vorlage für andere Sachbearbeitung."}) : null});
  await state.openSystem();
  await state.choose(901);
  const body = state.elements.invoiceDeliveryBody;
  body.value = "Zusätzliche Infos\n<b>Wörtlich</b> & {{ invoice_number }}";
  await state.fire("invoiceDeliveryBody", "input");
  await state.choose(902);
  assert.equal(body.value, "Zusätzliche Infos\n<b>Wörtlich</b> & {{ invoice_number }}");
  state.elements.invoiceDeliverySystem.checked = false;
  await state.fire("invoiceDeliveryClient", "change");
  state.elements.invoiceDeliverySystem.checked = true;
  await state.fire("invoiceDeliverySystem", "change");
  assert.match(body.value, /Zusätzliche Infos/);
  await state.fire("invoiceDeliveryResetBody", "click");
  assert.equal(body.value, "Vorlage für andere Sachbearbeitung.");
  await state.choose(901);
  assert.equal(body.value, "Bitte bearbeiten.");
  body.value = "";
  await state.fire("invoiceDeliveryBody", "input");
  await state.choose(902);
  assert.equal(body.value, "");
});

test("sending freezes the edited text and disables the other dispatch modes", async () => {
  let resolve;
  const state = fixture({fetchResult: async url => url.pathname === "/send" ? new Promise(done => { resolve = done; }) : null});
  await state.openSystem();
  await state.choose(901);
  const body = state.elements.invoiceDeliveryBody;
  body.value = "Spezielle Hinweise\nBitte beachten.";
  await state.fire("invoiceDeliveryBody", "input");
  const sending = state.fire("invoiceDeliverySend", "click");
  await settle();
  assert.equal(body.readOnly, true);
  assert.equal(state.elements.invoiceDeliveryResetBody.disabled, true);
  await state.fire("invoiceDeliveryResetBody", "click");
  assert.equal(body.value, "Spezielle Hinweise\nBitte beachten.");
  await state.fire("invoiceDeliverySend", "click");
  const requests = state.calls.filter(call => call.url.pathname === "/send");
  assert.equal(requests.length, 1);
  assert.equal(JSON.parse(requests[0].body).body, body.value);
  resolve(response({id: 12, state: "pending", recipient: "office@example.invalid"}));
  await sending;
  assert.equal(body.readOnly, true);
  assert.equal(state.elements.invoiceDeliveryModes.disabled, true);
  assert.equal(body.value, "Spezielle Hinweise\nBitte beachten.");
});

test("invalid text fields are checked before a mail is queued", async () => {
  const state = fixture();
  await state.openSystem();
  await state.choose(901);
  state.elements.invoiceDeliveryBody.reportValidity = () => false;
  await state.fire("invoiceDeliverySend", "click");
  assert.equal(state.calls.some(call => call.url.pathname === "/send"), false);
  assert.equal(state.elements.invoiceDeliverySend.disabled, false);
});

test("print mode shows only the print panel", async () => {
  const state = fixture();
  await state.openSystem();
  state.elements.invoiceDeliverySystem.checked = false;
  state.elements.invoiceDeliveryPrint.checked = true;
  await state.fire("invoiceDeliveryPrint", "change");
  assert.equal(state.elements.invoiceDeliverySystemPanel.hidden, true);
  assert.equal(state.elements.invoiceDeliveryClientPanel.hidden, true);
  assert.equal(state.elements.invoiceDeliveryPrintPanel.hidden, false);
});
