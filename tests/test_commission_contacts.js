const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../app/static/js/commission_contacts.js"), "utf8");

class Element {
    constructor() {
        this.value = "";
        this.textContent = "";
        this.children = [];
        this.listeners = {};
        this.dataset = {};
        this.disabled = false;
        this.classes = new Set();
        this.classList = {
            toggle: (name, enabled) => enabled ? this.classes.add(name) : this.classes.delete(name),
            contains: name => this.classes.has(name),
        };
    }
    addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler); }
    trigger(name) { return Promise.all((this.listeners[name] || []).map(handler => handler())); }
    click() { return this.disabled ? Promise.resolve() : this.trigger("click"); }
    setAttribute(name, value) { this[name] = value; }
    replaceChildren(...children) { this.children = children; }
    appendChild(child) { this.children.push(child); }
    append(...children) { this.children.push(...children); }
    cloneNode() { return Object.assign(new Element(), {value: this.value, textContent: this.textContent}); }
    focus() {}
}

function form(type = "license_agreement", savedText = "") {
    const elements = Object.fromEntries([
        "commissionForm", "otherAffectedModal", "reporterField", "reporter", "otherAffectedField", "otherAffectedValue",
        "otherAffectedButton", "otherAffectedIcon", "affectedEditor", "affectedSearch", "affectedSearchContext",
        "affectedResults", "affectedContactStatus", "applyAffected", "objectSearchContext", "objectType", "objectSummary",
    ].map(name => [name, new Element()]));
    elements.objectType.value = type;
    elements.objectSearchContext.value = "tenant";
    elements.objectSearchContext.options = ["tenant", "address", "agreement", "use_unit", "economic_unit", "all"].map(value => Object.assign(new Element(), {value}));
    elements.otherAffectedValue.value = savedText;
    elements.commissionForm.dataset = {objectSearchUrl: "/objects/search", affectedContactsUrl: "/admin/commission-objects/0/affected-contacts"};
    const timers = new Map();
    let timerId = 0;
    const requests = [];
    const state = {elements, requests, items: [], texts: {4: "Mietvertrag: MV-4\nErika\nTelefon: 123", 5: "Mietvertrag: MV-5\nMax"}};
    const sandbox = {
        URL,
        window: {location: {origin: "https://example.invalid"}},
        document: {
            getElementById: name => elements[name],
            createElement: () => new Element(),
            addEventListener: (name, handler) => handler(),
        },
        bootstrap: {Modal: {getInstance: () => ({hide: () => elements.otherAffectedModal.trigger("hide.bs.modal")})}},
        setTimeout: handler => { timers.set(++timerId, handler); return timerId; },
        clearTimeout: id => timers.delete(id),
        fetch: async url => {
            requests.push(url);
            if (state.fetch) return state.fetch(url);
            const identity = /\/(\d+)\/affected-contacts$/.exec(url)?.[1];
            return {ok: true, json: async () => identity ? {text: state.texts[identity]} : {items: state.items}};
        },
    };
    vm.runInNewContext(source, sandbox);
    state.open = () => elements.otherAffectedModal.trigger("show.bs.modal");
    state.close = () => elements.otherAffectedModal.trigger("hide.bs.modal");
    state.search = async (query, context = "tenant") => {
        elements.affectedSearch.value = query;
        elements.affectedSearchContext.value = context;
        await elements.affectedSearch.trigger("input");
        const handlers = Array.from(timers.values());
        timers.clear();
        await Promise.all(handlers.map(handler => handler()));
    };
    state.selectType = async type => {
        elements.objectType.value = type;
        await elements.objectSummary.trigger("commission-object-selected");
    };
    return state;
}

const building = {object_type: "building", object_id: 2, label: "Musterstraße 1", building: {address: "Musterstraße 1"}};
const agreement = identity => ({object_type: "license_agreement", object_id: identity, label: `Mieter ${identity}`, license_agreement: {id_num: `MV-${identity}`}});

test("the selected scope shows only the appropriate fields and disables irrelevant saved values", async () => {
    const state = form("");
    const fields = state.elements;
    for (const type of ["", "building", "economic_unit", "use_unit", "license_agreement"]) {
        await state.selectType(type);
        const reporter = ["building", "economic_unit"].includes(type);
        const affected = ["use_unit", "license_agreement"].includes(type);
        assert.equal(fields.reporterField.classList.contains("d-none"), !reporter);
        assert.equal(fields.reporter.disabled, !reporter);
        assert.equal(fields.otherAffectedField.classList.contains("d-none"), !affected);
        assert.equal(fields.otherAffectedValue.disabled, !affected);
    }
});

test("search keeps all main search contexts, uses the same endpoint, and only permits contracts", async () => {
    const state = form();
    assert.deepEqual(state.elements.affectedSearchContext.children.map(option => option.value), state.elements.objectSearchContext.options.map(option => option.value));
    state.items = [building, agreement(4)];
    await state.open();
    await state.search("Musterstraße", "address");
    const url = new URL(state.requests[0]);
    assert.equal(url.pathname, "/objects/search");
    assert.equal(url.searchParams.get("context"), "address");
    const [buildingButton, contractButton] = state.elements.affectedResults.children;
    assert.equal(buildingButton.disabled, true);
    assert.equal(contractButton.disabled, false);
    await contractButton.click();
    assert.match(state.elements.affectedEditor.value, /Mietvertrag: MV-4/);
    assert.equal(state.elements.objectType.value, "license_agreement");
});

test("short searches do not send requests", async () => {
    const state = form();
    await state.open();
    await state.search("a");
    assert.equal(state.requests.length, 0);
});

test("blocks are separated by five dashes, remain editable, and only apply changes the button", async () => {
    const state = form();
    state.items = [agreement(4), agreement(5)];
    await state.open();
    await state.search("Mieter");
    await state.elements.affectedResults.children[0].click();
    await state.elements.affectedResults.children[1].click();
    assert.equal(state.elements.affectedEditor.value, `${state.texts[4]}\n-----\n${state.texts[5]}`);
    assert.equal(state.elements.otherAffectedValue.value, "");
    assert.equal(state.elements.otherAffectedButton.classList.contains("btn-success"), false);
    state.elements.affectedEditor.value += "\nEigener Hinweis";
    await state.elements.applyAffected.click();
    assert.match(state.elements.otherAffectedValue.value, /Eigener Hinweis$/);
    assert.equal(state.elements.otherAffectedButton.classList.contains("btn-success"), true);
    assert.match(state.elements.otherAffectedIcon.className, /check-circle/);
    await state.open();
    assert.equal(state.elements.affectedEditor.value, state.elements.otherAffectedValue.value);
});

test("cancel discards draft edits and clearing the saved text resets the button", async () => {
    const state = form("use_unit", "Vorhandene Angaben");
    await state.open();
    state.elements.affectedEditor.value = "Verworfener Entwurf";
    await state.close();
    await state.open();
    assert.equal(state.elements.affectedEditor.value, "Vorhandene Angaben");
    state.elements.affectedEditor.value = " \n ";
    await state.elements.applyAffected.click();
    assert.equal(state.elements.otherAffectedValue.value, "");
    assert.equal(state.elements.otherAffectedButton.classList.contains("btn-success"), false);
});

test("contact lookup errors preserve edited text and allow another attempt", async () => {
    const state = form("license_agreement", "Eigener Hinweis");
    state.items = [agreement(4)];
    await state.open();
    await state.search("Mieter");
    state.fetch = async () => ({ok: false, json: async () => ({message: "Verbindung fehlgeschlagen"})});
    const button = state.elements.affectedResults.children[0];
    await button.click();
    assert.equal(state.elements.affectedEditor.value, "Eigener Hinweis");
    assert.equal(state.elements.affectedContactStatus.textContent, "Verbindung fehlgeschlagen");
    assert.equal(button.disabled, false);
    assert.equal(state.elements.applyAffected.disabled, false);
});

test("an outstanding contact request cannot alter a reopened draft or enable apply too early", async () => {
    const state = form();
    state.items = [agreement(4)];
    await state.open();
    await state.search("Mieter");
    let resolve;
    state.fetch = () => new Promise(complete => { resolve = complete; });
    const request = state.elements.affectedResults.children[0].click();
    assert.equal(state.elements.applyAffected.disabled, true);
    await state.close();
    await state.open();
    state.elements.affectedEditor.value = "Neuer Entwurf";
    resolve({ok: true, json: async () => ({text: state.texts[4]})});
    await request;
    assert.equal(state.elements.affectedEditor.value, "Neuer Entwurf");
});

test("a stale result cannot replace a more recent search", async () => {
    const state = form();
    await state.open();
    let resolve;
    let notifyStarted;
    const started = new Promise(complete => { notifyStarted = complete; });
    state.fetch = () => new Promise(complete => { resolve = complete; notifyStarted(); });
    const earlier = state.search("Alter Mieter");
    await started;
    state.fetch = async () => ({ok: true, json: async () => ({items: [agreement(5)]})});
    await state.search("Neuer Mieter");
    resolve({ok: true, json: async () => ({items: [agreement(4)]})});
    await earlier;
    assert.equal(state.elements.affectedResults.children[0].children[0].textContent, "Mieter 5");
});
