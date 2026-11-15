const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

class Element {
    constructor(tag = "div") { this.tag = tag; this.children = []; this.dataset = {}; this.listeners = {}; this.value = ""; this._text = ""; }
    set textContent(value) { this._text = String(value); this.children = []; }
    get textContent() { return this._text + this.children.map(child => child.textContent).join(""); }
    set innerHTML(value) { throw new Error("Unsafe HTML insertion: " + value); }
    appendChild(child) { this.children.push(child); }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this._text = ""; this.children = children; }
    addEventListener(name, listener) { this.listeners[name] = listener; }
    setAttribute(name, value) { this[name] = value; }
}

const source = fs.readFileSync(path.join(__dirname, "../app/static/js/commission_object_history.js"), "utf8");
const building = {object_type: "building", object_id: 2, label: "Musterstraße 1"};
const row = {id: 1, number: "01-2026-12345", title: "Wasserhahn", description: "Tropft in der Küche", is_open: true, status: "In Bearbeitung", created_at: "15.11.2026 10:00", created_by: "Andere Sachbearbeiterin", craftsman: "Regie", url: "/admin/commissions/1"};
const result = {total: 3, open_count: 1, page: 1, pages: 1, items: [row]};
const response = payload => ({ok: true, json: async () => payload});
const settle = () => new Promise(resolve => setImmediate(resolve));

function setup(handler = () => response(result)) {
    const elements = Object.fromEntries([
        "commissionForm", "objectSummary", "objectCommissionModal", "objectCommissionList", "objectCommissionStatus",
        "objectCommissionSelection", "objectCommissionView", "objectCommissionPrevious", "objectCommissionNext",
        "objectCommissionPage", "objectCommissionRefresh", "positionText", "craftActivity", "notifyTenants",
    ].map(name => [name, new Element()]));
    elements.commissionForm.dataset.objectCommissionsUrl = "/admin/commission-objects/commissions";
    elements.positionText.value = "Ungespeicherter Auftragstext";
    elements.craftActivity.value = "123";
    elements.notifyTenants.value = "false";
    const calls = [];
    const sandbox = {
        document: {getElementById: name => elements[name], createElement: tag => new Element(tag), addEventListener: (name, listener) => listener()},
        window: {location: {origin: "https://example.invalid"}}, URL, AbortController, setTimeout, clearTimeout,
        fetch: async url => { const parsed = new URL(url); calls.push(parsed); return handler(parsed); },
    };
    vm.runInNewContext(source, sandbox);
    return {
        elements, calls,
        select: item => { elements.objectSummary.replaceChildren(); return elements.objectSummary.listeners["commission-object-selected"]({detail: item}); },
        async fire(name, event) { await elements[name].listeners[event](); await settle(); },
    };
}

test("selection checks the exact object and highlights open orders without changing the draft", async () => {
    const state = setup();
    await state.select(building);
    const button = state.elements.objectSummary.children[0];
    assert.equal(button.type, "button");
    assert.match(button.textContent, /1 offen/);
    assert.match(button.className, /btn-outline-warning/);
    assert.equal(button["data-bs-target"], "#objectCommissionModal");
    assert.equal(state.calls[0].searchParams.get("object_type"), "building");
    assert.equal(state.calls[0].searchParams.get("object_id"), "2");
    assert.equal(state.calls[0].searchParams.get("summary"), "true");
    await state.fire("objectCommissionModal", "show.bs.modal");
    assert.equal(state.elements.objectCommissionView.value, "open");
    const card = state.elements.objectCommissionList.children[0];
    assert.match(card.textContent, /Tropft in der Küche/);
    assert.match(card.textContent, /Andere Sachbearbeiterin/);
    const link = card.children[0].children[0];
    assert.equal(link.target, "_blank");
    assert.equal(link.rel, "noopener noreferrer");
    await state.fire("objectCommissionModal", "hidden.bs.modal");
    assert.equal(state.elements.positionText.value, "Ungespeicherter Auftragstext");
    assert.equal(state.elements.craftActivity.value, "123");
    assert.equal(state.elements.notifyTenants.value, "false");
});

test("completed orders remain available and an empty history is explicit", async () => {
    for (const total of [0, 3]) {
        const state = setup(() => response({...result, total, open_count: 0, items: total ? [{...row, is_open: false}] : []}));
        await state.select(building);
        assert.match(state.elements.objectSummary.textContent, new RegExp(`${total} insgesamt`));
        assert.doesNotMatch(state.elements.objectSummary.children[0].className, /warning/);
        await state.fire("objectCommissionModal", "show.bs.modal");
        assert.equal(state.elements.objectCommissionView.value, "all");
        assert.match(state.elements.objectCommissionStatus.textContent, total ? /0 offene von 3/ : /noch keine Aufträge/);
        if (total) assert.match(state.elements.objectCommissionList.textContent, /Abgeschlossen/);
    }
});

test("all orders can be paged and refreshed without leaving the form", async () => {
    const state = setup(url => response({...result, page: Number(url.searchParams.get("page") || 1), pages: 2}));
    await state.select(building);
    await state.fire("objectCommissionModal", "show.bs.modal");
    state.elements.objectCommissionView.value = "all";
    await state.fire("objectCommissionView", "change");
    assert.equal(state.calls.at(-1).searchParams.get("view"), "all");
    await state.fire("objectCommissionNext", "click");
    assert.equal(state.calls.at(-1).searchParams.get("page"), "2");
    assert.equal(state.elements.objectCommissionNext.disabled, true);
    assert.equal(state.elements.objectCommissionPrevious.disabled, false);
    await state.fire("objectCommissionRefresh", "click");
    assert.equal(state.calls.at(-1).searchParams.get("page"), "2");
    assert.equal(state.elements.positionText.value, "Ungespeicherter Auftragstext");
});

test("late counts from a previous selection cannot replace the current selection", async () => {
    let finish;
    const state = setup(url => url.searchParams.get("object_id") === "2" ? new Promise(resolve => {finish = resolve;}) : response({...result, open_count: 2}));
    const pending = state.select(building);
    await state.select({object_type: "license_agreement", object_id: 44, label: "Mieter"});
    finish(response({...result, open_count: 99}));
    await pending;
    assert.match(state.elements.objectSummary.textContent, /2 offen/);
    assert.doesNotMatch(state.elements.objectSummary.textContent, /99/);
    assert.match(state.elements.objectCommissionSelection.textContent, /Mieter · Vertrag/);
});

test("closing the dialog ignores an outstanding page and late counts do not overwrite newer counts", async () => {
    let finishCounts;
    let finishPage;
    const state = setup(url => new Promise(resolve => {
        if (url.searchParams.get("summary")) finishCounts = resolve;
        else finishPage = resolve;
    }));
    const pending = state.select(building);
    await state.fire("objectCommissionModal", "show.bs.modal");
    finishPage(response({...result, open_count: 4}));
    await settle();
    finishCounts(response({...result, open_count: 99}));
    await pending;
    assert.match(state.elements.objectSummary.textContent, /4 offen/);
    const refreshing = state.fire("objectCommissionRefresh", "click");
    await settle();
    await state.fire("objectCommissionModal", "hidden.bs.modal");
    finishPage(response({...result, items: [{...row, title: "Veraltete Antwort"}]}));
    await refreshing;
    await settle();
    assert.doesNotMatch(state.elements.objectCommissionList.textContent, /Veraltete Antwort/);
});

test("lookup errors are visible and retryable and never claim there are no open orders", async () => {
    let failed = true;
    const state = setup(() => failed ? {ok: false, json: async () => ({message: "Aufträge nicht verfügbar"})} : response(result));
    await state.select(building);
    assert.match(state.elements.objectSummary.textContent, /Prüfung fehlgeschlagen/);
    assert.doesNotMatch(state.elements.objectSummary.textContent, /0 insgesamt/);
    await state.fire("objectCommissionModal", "show.bs.modal");
    assert.match(state.elements.objectCommissionStatus.textContent, /nicht verfügbar/);
    failed = false;
    await state.fire("objectCommissionRefresh", "click");
    assert.match(state.elements.objectCommissionList.textContent, /Tropft/);
});

test("closing before either request completes still updates the button with the newest counts", async () => {
    let finishCounts;
    let finishPage;
    const state = setup(url => new Promise(resolve => {
        if (url.searchParams.get("summary")) finishCounts = resolve;
        else finishPage = resolve;
    }));
    const pending = state.select(building);
    await state.fire("objectCommissionModal", "show.bs.modal");
    await state.fire("objectCommissionModal", "hidden.bs.modal");
    finishPage(response({...result, open_count: 4}));
    await settle();
    finishCounts(response({...result, open_count: 99}));
    await pending;
    assert.match(state.elements.objectSummary.textContent, /4 offen/);
    assert.doesNotMatch(state.elements.objectSummary.textContent, /99/);
    assert.doesNotMatch(state.elements.objectCommissionList.textContent, /Wasserhahn/);
});

test("order text is rendered as plain text", async () => {
    const state = setup(() => response({...result, items: [{...row, description: '<img src=x onerror="alert(1)">'}]}));
    await state.select(building);
    await state.fire("objectCommissionModal", "show.bs.modal");
    assert.match(state.elements.objectCommissionList.textContent, /<img src=x/);
});
