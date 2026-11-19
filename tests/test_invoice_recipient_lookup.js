const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../app/static/js/recipient_lookup.js"), "utf8");
const contract = {tenant_name: "Testmieterin", company_code: {code: "01"}, license_agreement: {erp_id: 101, id_num: "MV-101"}};
const firstParty = {person_id: 5000, name: "Testmieterin", role: "Mieterin"};

class Element {
  constructor() {
    this.value = "";
    this.dataset = {};
    this.children = [];
    this.events = [];
    this.listeners = {};
    this.classList = {add() {}, remove() {}, toggle() {}};
  }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  dispatchEvent(event) {
    this.events.push(event);
    for (const callback of this.listeners[event.type] || []) callback(event);
  }
  appendChild(child) { this.children.push(child); }
  add(option) { this.children.push(option); }
  replaceChildren(...children) { this.children = children; this.value = ""; }
}

function fixture({assigning = true, parties = [firstParty], assignment = {issuer_name: "GmbH", recipient_name: "eG"}} = {}) {
  const ids = ["tenantSearch", "tenantSearchResults", "selectedAgreement", "invoiceAgreementSaveNotice", "recipientLicenseAgreementErpId",
    "recipientPersonErpId", "recipientMessage", "recipientCandidateContainer", "recipientCandidateSelect", "invoiceRecipientFields",
    "invoicePaymentInformation", "invoiceFinalize", "recipientType", "recipientName", "recipientNameAddition", "recipientStreet",
    "recipientPostalCode", "recipientTown", "recipientCountryCode", "recipientEmail", "recipientVatId"];
  if (assigning) ids.push("pendingInvoiceAgreement", "pendingInvoiceAgreementPerson", "invoiceAgreementAssignment");
  const elements = Object.fromEntries(ids.map(id => [id, new Element()]));
  const form = new Element();
  const search = elements.tenantSearch;
  search.id = "tenantSearch";
  search.dataset = {documentType: "invoice", searchUrl: "/search", recipientUrl: "/recipients"};
  search.closest = () => form;
  elements.invoiceAgreementSaveNotice.hidden = true;
  elements.selectedAgreement.textContent = assigning ? "Kein Vertrag ausgewählt" : "Vertrag MV-99";
  elements.recipientLicenseAgreementErpId.value = assigning ? "" : "99";
  elements.recipientName.value = "Bisheriger Empfänger";

  const event = class { constructor(type, options) { this.type = type; this.detail = options?.detail; } };
  vm.runInNewContext(source, {
    document: {
      addEventListener: (_, callback) => callback(),
      getElementById: id => elements[id],
      querySelectorAll: () => [search],
      createElement: () => new Element(),
    },
    window: {location: {origin: "https://example.invalid"}},
    URL, AbortController, setTimeout, clearTimeout, Event: event, CustomEvent: event,
    Option: class { constructor(label, value) { this.label = label; this.value = value; } },
    fetch: async url => ({ok: true, json: async () => url.pathname === "/search" ? {items: [contract]} : {items: parties, assignment}}),
  });
  const settle = () => new Promise(resolve => setImmediate(resolve));
  return {
    elements, form,
    async select() {
      search.value = "MV-101";
      search.dispatchEvent({type: "keydown", key: "Enter", preventDefault() {}});
      await settle();
      elements.tenantSearchResults.children[0].dispatchEvent({type: "click"});
      await settle();
    },
    submit() {
      let prevented = false;
      form.dispatchEvent({type: "submit", preventDefault: () => { prevented = true; }});
      return !prevented;
    },
  };
}

test("the invoice shows the save hint and stages a contract without loading the old issuer's payment data", async () => {
  const state = fixture();
  await state.select();
  const elements = state.elements;
  assert.match(elements.selectedAgreement.textContent, /MV-101/);
  assert.equal(elements.invoiceAgreementSaveNotice.hidden, false);
  assert.equal(elements.invoiceAgreementAssignment.textContent, "Aussteller GmbH · Rechnungsempfänger eG");
  assert.equal(elements.pendingInvoiceAgreement.value, "101");
  assert.equal(elements.pendingInvoiceAgreementPerson.value, "5000");
  assert.equal(elements.recipientLicenseAgreementErpId.value, "");
  assert.equal(elements.invoiceRecipientFields.disabled, true);
  assert.equal(elements.invoicePaymentInformation.hidden, true);
  assert.equal(elements.invoiceFinalize.disabled, true);
  assert.equal(state.form.events.some(event => event.type === "invoice-agreement-selected"), false);
  assert.equal(state.submit(), true);
});

test("multiple parties require a choice and clearing the search drops an unsaved contract", async () => {
  const state = fixture({parties: [firstParty, {person_id: 5001, name: "Zweiter Mieter"}], assignment: {issuer_name: "eG", recipient_name: null}});
  await state.select();
  const elements = state.elements;
  assert.equal(elements.recipientCandidateSelect.required, true);
  assert.equal(state.submit(), false);
  elements.recipientCandidateSelect.value = "1";
  elements.recipientCandidateSelect.dispatchEvent({type: "change"});
  assert.equal(elements.pendingInvoiceAgreementPerson.value, "5001");
  assert.match(elements.invoiceAgreementAssignment.textContent, /Zweiter Mieter/);
  assert.equal(state.submit(), true);
  elements.tenantSearch.value = "";
  elements.tenantSearch.dispatchEvent({type: "input"});
  assert.equal(elements.pendingInvoiceAgreement.value, "");
  assert.equal(elements.pendingInvoiceAgreementPerson.value, "");
  assert.equal(elements.invoiceAgreementSaveNotice.hidden, true);
  assert.equal(elements.invoiceFinalize.disabled, false);
  assert.equal(elements.invoicePaymentInformation.hidden, false);
  assert.equal(elements.recipientName.value, "Bisheriger Empfänger");
});

test("an existing direct invoice still loads the selected recipient and refreshes its payments", async () => {
  const state = fixture({assigning: false, assignment: null});
  await state.select();
  assert.equal(state.elements.recipientLicenseAgreementErpId.value, "101");
  assert.equal(state.elements.recipientName.value, firstParty.name);
  assert.equal(state.elements.invoiceAgreementSaveNotice.hidden, false);
  assert.equal(state.form.events.find(event => event.type === "invoice-agreement-selected").detail.agreementId, "101");
  assert.equal(state.submit(), true);
});
