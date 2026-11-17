const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../app/static/js/invoice_payments.js"), "utf8");
const transfer = {method: "bank_transfer", iban: "DE89370400440532013000", bic: "COBADEFFXXX", source: "company"};
const debit = {method: "sepa_debit", iban: "DE12500105170648489890", bic: "INGDDEFFXXX", mandate_reference: "MANDAT-1", creditor_id: "DE98ZZZ09999999999", license_agreement_erp_id: 101, source: "wowiport"};

class Element {
  constructor() { this.value = ""; this.dataset = {}; this.listeners = {}; this.children = []; this.hidden = false; this.disabled = false; this.events = []; }
  addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler); }
  dispatchEvent(event) { this.events.push(event); (this.listeners[event.type] || []).forEach(handler => handler(event)); }
  replaceChildren(...children) { this.children = children; this.value = ""; }
  add(option) { this.children.push(option); }
  querySelectorAll() { return []; }
}

function fixture({initial = transfer, agreement = "", fetchResult} = {}) {
  const ids = ["invoicePaymentInformation", "invoicePaymentEdit", "invoice-draft-form", "invoicePaymentData", "recipientLicenseAgreementErpId", "invoicePaymentModal", "invoicePaymentRefresh", "invoicePaymentMessage", "paymentMethod", "paymentOption", "paymentEditError", "invoicePaymentMethod", "invoicePaymentAccountLabel", "invoicePaymentIban", "invoicePaymentBic", "invoicePaymentMandate", "invoicePaymentCreditor", "paymentAccountHelp", "paymentApply", "paymentIban", "paymentBic", "paymentMandateReference", "paymentCreditorId", "paymentAccountHolder"];
  const elements = Object.fromEntries(ids.map(id => [id, new Element()]));
  elements.invoicePaymentData.value = JSON.stringify(initial);
  elements.recipientLicenseAgreementErpId.value = agreement;
  elements.invoicePaymentInformation.dataset = {endpoint: "/payments", creditorId: "DE98ZZZ09999999999"};
  const calls = [];
  const timers = new Map();
  let timerId = 0;
  let hidden = 0;
  vm.runInNewContext(source, {
    document: {getElementById: id => elements[id], addEventListener: (name, handler) => handler()},
    window: {location: {origin: "https://example.invalid"}}, URL, AbortController,
    setTimeout: handler => { timers.set(++timerId, handler); return timerId; }, clearTimeout: id => timers.delete(id),
    Event: class { constructor(type, options) { this.type = type; this.bubbles = options?.bubbles; } },
    Option: class { constructor(text, value) { this.textContent = text; this.value = value; } },
    bootstrap: {Modal: {getOrCreateInstance: () => ({hide: () => { hidden += 1; }})}},
    fetch: async (url, options) => { calls.push({url: new URL(url), ...options}); return fetchResult ? fetchResult(url, options) : response({selected: debit, options: [debit, transfer]}); }
  });
  const fire = async (id, name, event = {}) => { for (const handler of elements[id].listeners[name] || []) await handler(event); };
  return {elements, calls, timers, fire, hidden: () => hidden,
    data: () => JSON.parse(elements.invoicePaymentData.value),
    select: async id => { elements.recipientLicenseAgreementErpId.value = String(id); await fire("invoice-draft-form", "invoice-agreement-selected"); }};
}

function response(data, ok = true) { return {ok, redirected: false, json: async () => data}; }
const settle = () => new Promise(resolve => setImmediate(resolve));

test("selecting a tenant fetches fresh data and replaces the previous account", async () => {
  const state = fixture();
  await state.select(101);
  assert.equal(state.calls[0].url.searchParams.get("license_agreement_id"), "101");
  assert.equal(state.calls[0].cache, "no-store");
  assert.equal(state.data().iban, debit.iban);
  assert.equal(state.elements.invoicePaymentAccountLabel.textContent, "Einzugskonto");
  assert.equal(state.elements.invoicePaymentEdit.disabled, false);
  assert.equal(state.timers.size, 0);
});

test("late results cannot overwrite another tenant's payment data", async () => {
  const replies = [];
  const state = fixture({fetchResult: () => new Promise(resolve => replies.push(resolve))});
  const first = state.select(101);
  const second = state.select(202);
  replies[1](response({selected: {...transfer, license_agreement_erp_id: 202}, options: [transfer]}));
  await second;
  replies[0](response({selected: debit, options: [debit]}));
  await first;
  assert.equal(state.data().license_agreement_erp_id, 202);
  assert.equal(state.data().iban, transfer.iban);
});

test("fetch failure clears the previous tenant's bank details and allows manual correction", async () => {
  const state = fixture({fetchResult: async () => response({message: "Wowiport nicht erreichbar"}, false)});
  await state.select(101);
  assert.equal(state.data().iban, "");
  assert.equal(state.data().source, "unresolved");
  assert.equal(state.elements.invoicePaymentMessage.textContent, "Wowiport nicht erreichbar");
  assert.equal(state.elements.invoicePaymentEdit.disabled, false);
});

test("switching payment methods never reuses the debit account as the transfer account", async () => {
  const state = fixture();
  await state.select(101);
  await state.fire("invoicePaymentModal", "show.bs.modal");
  assert.equal(state.elements.paymentIban.value, debit.iban);
  state.elements.paymentMethod.value = "bank_transfer";
  await state.fire("paymentMethod", "change");
  assert.equal(state.elements.paymentIban.value, transfer.iban);
  await state.fire("paymentApply", "click");
  assert.equal(state.data().method, "bank_transfer");
  assert.equal(state.data().iban, transfer.iban);
  assert.equal(state.data().source, "manual");
  assert.equal(state.hidden(), 1);
});

test("manual data is applied only after confirmation and an invalid IBAN is rejected", async () => {
  const state = fixture();
  await state.fire("invoicePaymentModal", "show.bs.modal");
  state.elements.paymentIban.value = "DE001234";
  await state.fire("paymentApply", "click");
  assert.equal(state.data().iban, transfer.iban);
  assert.equal(state.elements.paymentEditError.hidden, false);
  state.elements.paymentIban.value = "DE89 3704 0044 0532 0130 00";
  await state.fire("paymentApply", "click");
  assert.equal(state.data().iban, transfer.iban);
  assert.equal(state.data().source, "manual");
});

test("opening a saved manual snapshot does not refetch it, unresolved data does", async () => {
  const manual = fixture({initial: {...debit, source: "manual"}, agreement: "101"});
  assert.equal(manual.calls.length, 0);
  const unresolved = fixture({initial: {method: "", source: "unresolved"}, agreement: "101"});
  await settle();
  assert.equal(unresolved.calls.length, 1);
  assert.equal(unresolved.data().iban, debit.iban);
});

test("saving is delayed while a fresh lookup is in progress", async () => {
  let resolve;
  const state = fixture({fetchResult: () => new Promise(done => { resolve = done; })});
  const loading = state.select(101);
  let prevented = false;
  await state.fire("invoice-draft-form", "submit", {preventDefault: () => { prevented = true; }});
  assert.equal(prevented, true);
  resolve(response({selected: debit, options: [debit]}));
  await loading;
});
