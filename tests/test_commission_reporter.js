const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../app/static/js/commission_reporter.js"), "utf8");

class Element {
    constructor(tag = "div") {
        this.tag = tag;
        this.value = "";
        this.textContent = "";
        this.children = [];
        this.listeners = {};
        this.dataset = {};
        this.classes = new Set();
        this.classList = {
            toggle: (name, enabled) => enabled ? this.classes.add(name) : this.classes.delete(name),
            contains: name => this.classes.has(name),
        };
    }
    addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler); }
    trigger(name, event = {}) { return Promise.all((this.listeners[name] || []).map(handler => handler(event))); }
    click() { return this.disabled ? Promise.resolve() : this.trigger("click"); }
    setAttribute(name, value) { this[name] = value; }
    replaceChildren(...children) { this.children = children; }
    appendChild(child) { this.children.push(child); }
    append(...children) { this.children.push(...children); }
    focus() {}
}

const tenants = [{contractor_id: 5005, name: "Erika Beispiel"}, {contractor_id: 6001, name: "Max Beispiel"}];
const contract = {object_type: "license_agreement", object_id: 4, license_agreement: {id_num: "MV-4"}, use_unit: {address: "Musterstraße 1"}, tenants};
const contact = tenant => ({agreement_id: 4, contractor_id: tenant.contractor_id, name: tenant.name, text: `${tenant.name}\nTelefon: ${tenant.contractor_id}\nE-Mail: mieter@example.invalid`});

function form(type = "building", initial = null) {
    const elements = Object.fromEntries([
        "commissionForm", "reporterSearch", "reporterField", "reporter", "reporterAgreement", "reporterContractor",
        "reporterResults", "reporterStatus", "reporterSelection", "reporterSummary", "clearReporter", "objectType", "objectSummary",
    ].map(name => [name, new Element()]));
    elements.objectType.value = type;
    elements.commissionForm.dataset = {reporterSearchUrl: "/reporters/search", reporterContactUrl: "/reporters/0/0"};
    if (initial) {
        elements.reporter.value = initial.text;
        elements.reporterAgreement.value = String(initial.agreement_id);
        elements.reporterContractor.value = String(initial.contractor_id);
    }
    const timers = new Map();
    let timerID = 0;
    const state = {elements, requests: [], items: [contract], delays: []};
    vm.runInNewContext(source, {
        URL,
        window: {location: {origin: "https://example.invalid"}},
        document: {getElementById: name => elements[name], createElement: tag => new Element(tag), addEventListener: (name, handler) => handler()},
        setTimeout: (handler, delay) => { state.delays.push(delay); timers.set(++timerID, handler); return timerID; },
        clearTimeout: identity => timers.delete(identity),
        fetch: async url => {
            state.requests.push(url);
            if (state.fetch) return state.fetch(url);
            const identity = /\/4\/(\d+)$/.exec(url)?.[1];
            return {ok: true, json: async () => identity ? {item: contact(tenants.find(tenant => String(tenant.contractor_id) === identity))} : {items: state.items}};
        },
    });
    state.flush = async () => {
        const handlers = Array.from(timers.values());
        timers.clear();
        await Promise.all(handlers.map(handler => handler()));
    };
    state.search = async query => {
        elements.reporterSearch.value = query;
        await elements.reporterSearch.trigger("input");
        await state.flush();
    };
    state.scope = async value => {
        elements.objectType.value = value;
        await elements.objectSummary.trigger("commission-object-selected");
    };
    return state;
}

test("reporter search is only available for buildings and economic units", async () => {
    const state = form();
    for (const type of ["building", "economic_unit", "use_unit", "license_agreement", ""]) {
        await state.scope(type);
        const enabled = ["building", "economic_unit"].includes(type);
        assert.equal(state.elements.reporterField.classList.contains("d-none"), !enabled);
        for (const name of ["reporterSearch", "reporter", "reporterAgreement", "reporterContractor"]) assert.equal(state.elements[name].disabled, !enabled);
    }
});

test("typing starts a debounced search without a search button and needs two characters", async () => {
    const state = form();
    await state.search("E");
    assert.equal(state.requests.length, 0);
    await state.search("Erika");
    const url = new URL(state.requests[0]);
    assert.equal(url.pathname, "/reporters/search");
    assert.equal(url.searchParams.get("q"), "Erika");
    assert.equal(state.delays[0], 300);
});

test("only contracts are shown with nonselectable headings and indented tenant buttons", async () => {
    const state = form();
    state.items.push({object_type: "building", object_id: 2, tenants});
    await state.search("MV-4");
    const [heading, first, second] = state.elements.reporterResults.children;
    assert.equal(state.elements.reporterResults.children.length, 3);
    assert.equal(heading.tag, "div");
    assert.equal(heading.children[0].textContent, "Mietvertrag MV-4");
    assert.equal(first.tag, "button");
    assert.match(first.className, /ps-4/);
    assert.equal(first.textContent, "Erika Beispiel");
    assert.equal(second.textContent, "Max Beispiel");
});

test("selecting a new reporter replaces the prior person and all displayed contact information", async () => {
    const state = form();
    await state.search("Erika");
    await state.elements.reporterResults.children[1].click();
    assert.equal(state.elements.reporter.value, contact(tenants[0]).text);
    assert.equal(state.elements.reporterContractor.value, "5005");
    await state.search("Max");
    await state.elements.reporterResults.children[2].click();
    assert.equal(state.elements.reporter.value, contact(tenants[1]).text);
    assert.equal(state.elements.reporterSummary.textContent, contact(tenants[1]).text);
    assert.equal(state.elements.reporterContractor.value, "6001");
    assert.doesNotMatch(state.elements.reporter.value, /Erika/);
});

test("a preselected reporter is shown and can be completely removed", async () => {
    const state = form("economic_unit", contact(tenants[0]));
    assert.equal(state.elements.reporterSelection.classList.contains("d-none"), false);
    assert.equal(state.elements.reporterSummary.textContent, contact(tenants[0]).text);
    await state.elements.clearReporter.click();
    for (const name of ["reporter", "reporterAgreement", "reporterContractor"]) assert.equal(state.elements[name].value, "");
    assert.equal(state.elements.reporterSelection.classList.contains("d-none"), true);
});

test("contact lookup failures keep the prior confirmed selection and allow another attempt", async () => {
    const state = form("building", contact(tenants[0]));
    await state.search("Max");
    state.fetch = async () => ({ok: false, json: async () => ({message: "Wowiport nicht erreichbar"})});
    await state.elements.reporterResults.children[2].click();
    assert.equal(state.elements.reporter.value, contact(tenants[0]).text);
    assert.equal(state.elements.reporterStatus.textContent, "Wowiport nicht erreichbar");
});

test("a late person response cannot overwrite a newer selection", async () => {
    const state = form();
    await state.search("Beispiel");
    let resolve;
    state.fetch = url => url.endsWith("/5005") ? new Promise(complete => { resolve = complete; }) : Promise.resolve({ok: true, json: async () => ({item: contact(tenants[1])})});
    const earlier = state.elements.reporterResults.children[1].click();
    await state.elements.reporterResults.children[2].click();
    resolve({ok: true, json: async () => ({item: contact(tenants[0])})});
    await earlier;
    assert.equal(state.elements.reporterContractor.value, "6001");
    assert.equal(state.elements.reporter.value, contact(tenants[1]).text);
});

test("leaving the reporter scope invalidates pending contact lookups", async () => {
    const state = form();
    await state.search("Beispiel");
    let resolve;
    state.fetch = () => new Promise(complete => { resolve = complete; });
    const request = state.elements.reporterResults.children[1].click();
    await state.scope("license_agreement");
    resolve({ok: true, json: async () => ({item: contact(tenants[0])})});
    await request;
    assert.equal(state.elements.reporter.value, "");
    assert.equal(state.elements.reporterContractor.disabled, true);
});

test("a stale search response cannot replace the results for a new query", async () => {
    const state = form();
    let resolve;
    let started;
    const pending = new Promise(complete => { started = complete; });
    state.fetch = () => new Promise(complete => { resolve = complete; started(); });
    const earlier = state.search("Erika");
    await pending;
    state.fetch = async () => ({ok: true, json: async () => ({items: [{...contract, tenants: [tenants[1]]}]})});
    await state.search("Max");
    resolve({ok: true, json: async () => ({items: [contract]})});
    await earlier;
    assert.equal(state.elements.reporterResults.children[1].textContent, "Max Beispiel");
});

test("enter searches without submitting and submission waits for pending contact information", async () => {
    const state = form();
    let prevented = false;
    state.elements.reporterSearch.value = "Erika";
    await state.elements.reporterSearch.trigger("keydown", {key: "Enter", preventDefault: () => { prevented = true; }});
    assert.equal(prevented, true);
    assert.equal(state.delays[0], 0);
    await state.flush();
    let resolve;
    state.fetch = () => new Promise(complete => { resolve = complete; });
    const request = state.elements.reporterResults.children[1].click();
    prevented = false;
    await state.elements.commissionForm.trigger("submit", {preventDefault: () => { prevented = true; }});
    assert.equal(prevented, true);
    resolve({ok: true, json: async () => ({item: contact(tenants[0])})});
    await request;
    prevented = false;
    await state.elements.commissionForm.trigger("submit", {preventDefault: () => { prevented = true; }});
    assert.equal(prevented, false);
});
