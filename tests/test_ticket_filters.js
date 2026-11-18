const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../app/static/js/ticket_filters.js"), "utf8");

function setup({withForm = true, withOwners = true, savedFocus = null, searchText = "", storageUnavailable = false,
    persistence = false, storageEntries = [], stateKey = "ticket-filters-1", currentUrl = "https://example.invalid/admin/tickets",
    canonicalUrl = "/admin/tickets?page=1&owner=mine&state=open&priority=all&department=all&ai_state=all&q="} = {}) {
    const listeners = () => ({listeners: {}, addEventListener(name, callback) { this.listeners[name] = callback; }});
    const summary = {...listeners(), setAttribute(name, value) { this[name] = value; }, focus() { this.focused = true; }};
    const reset = listeners();
    const owners = ["all", "mine", "erp:11", "erp:12"].map(value => ({
        value, checked: value === "mine", name: "owner",
        matches() { return true; },
        closest() { return {querySelector: () => ({textContent: value === "all" ? "Alle Sachbearbeiter" : value})}; },
    }));
    const search = {
        ...listeners(), value: searchText, selectionStart: 0, selectionEnd: 0, matches() { return false; },
        focus() { document.activeElement = this; },
        setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; },
    };
    const filter = {
        ...listeners(), open: false, contains: target => owners.includes(target) || target === summary,
        querySelector: () => summary, querySelectorAll: () => owners,
    };
    const submissions = [];
    const form = {...listeners(), dataset: persistence ? {stateKey, currentUrl: canonicalUrl} : {}, requestSubmit() {
        this.listeners.submit?.();
        submissions.push({owners: owners.filter(owner => owner.checked).map(owner => owner.value), search: search.value});
    }};
    const timers = new Map();
    let nextTimer = 0;
    const document = {
        ...listeners(), getElementById: id => ({ticketFilters: withForm ? form : null, ticketSearch: search, ticketOwnerFilter: withOwners ? filter : null, ticketFilterReset: reset}[id]),
    };
    const storage = new Map(storageEntries);
    if (savedFocus) storage.set("ticketSearchFocus", JSON.stringify(savedFocus));
    const navigations = [];
    const history = [];
    const sessionStorage = {
        getItem(key) { if (storageUnavailable) throw new Error("unavailable"); return storage.get(key) || null; },
        setItem(key, value) { if (storageUnavailable) throw new Error("unavailable"); storage.set(key, value); },
        removeItem(key) { storage.delete(key); },
    };
    vm.runInNewContext(source, {
        document, URL, URLSearchParams, window: {
            sessionStorage, location: {href: currentUrl, replace(url) { navigations.push(url); }},
            history: {replaceState(state, title, url) { history.push(url); }},
        },
        setTimeout(callback, delay) { const id = ++nextTimer; timers.set(id, {callback, delay}); return id; },
        clearTimeout(id) { timers.delete(id); },
    });
    document.listeners.DOMContentLoaded();
    return {
        form, search, filter, summary, reset, owners, submissions, timers, document, storage, navigations, history,
        type(value) { search.value = value; search.listeners.input(); },
        change(target) {
            if (withOwners) filter.listeners.change({target});
            form.listeners.change({target});
        },
        flush() {
            const pending = [...timers.values()];
            timers.clear();
            pending.forEach(timer => timer.callback());
        },
    };
}

test("select filters apply immediately and cancel an outstanding text search", () => {
    const state = setup();
    state.type("Wasserhahn");
    state.change({matches: () => true});
    assert.equal(state.submissions.length, 1);
    assert.equal(state.submissions[0].search, "Wasserhahn");
    state.flush();
    assert.equal(state.submissions.length, 1);
});

test("employee changes submit the normalized multiple selection without an apply button", () => {
    const state = setup();
    state.owners[2].checked = true;
    state.change(state.owners[2]);
    assert.deepEqual(state.submissions.at(-1).owners, ["mine", "erp:11"]);
    assert.equal(state.summary.textContent, "2 ausgewählt");
    state.owners[0].checked = true;
    state.change(state.owners[0]);
    assert.deepEqual(state.submissions.at(-1).owners, ["all"]);
    assert.equal(state.summary.textContent, "Alle Sachbearbeiter");
    state.owners[3].checked = true;
    state.change(state.owners[3]);
    assert.deepEqual(state.submissions.at(-1).owners, ["erp:12"]);
    state.owners[3].checked = false;
    state.change(state.owners[3]);
    assert.deepEqual(state.submissions.at(-1).owners, []);
    assert.equal(state.submissions.length, 4);
});

test("typing waits for a short pause and submits the latest search once", () => {
    const state = setup();
    state.type("Was");
    state.type("Wasser");
    state.type("Wasserhahn");
    assert.equal(state.submissions.length, 0);
    assert.equal(state.timers.size, 1);
    assert.equal([...state.timers.values()][0].delay, 350);
    state.flush();
    assert.equal(state.submissions.length, 1);
    assert.equal(state.submissions[0].search, "Wasserhahn");
    state.type("");
    state.flush();
    assert.equal(state.submissions.at(-1).search, "");
});

test("an explicit submit cancels the pending automatic search", () => {
    const state = setup();
    state.type("Rückfrage");
    state.form.requestSubmit();
    state.flush();
    assert.equal(state.submissions.length, 1);
    assert.equal(state.submissions[0].search, "Rückfrage");
});

test("unfinished composed text is not submitted", () => {
    const state = setup();
    state.type("M");
    state.search.listeners.compositionstart();
    state.type("Mü");
    state.flush();
    assert.equal(state.submissions.length, 0);
    state.search.listeners.compositionend();
    state.type("Müller");
    state.flush();
    assert.equal(state.submissions.length, 1);
    assert.equal(state.submissions[0].search, "Müller");
});

test("closing the employee selector still preserves keyboard and outside-click behavior", () => {
    const state = setup();
    state.filter.open = true;
    state.filter.listeners.keydown({key: "Escape"});
    assert.equal(state.filter.open, false);
    assert.equal(state.summary.focused, true);
    state.filter.open = true;
    state.document.listeners.click({target: {}});
    assert.equal(state.filter.open, false);
    assert.equal(state.submissions.length, 0);
});

test("text search also works without an employee selector and other pages are ignored", () => {
    const state = setup({withOwners: false});
    state.type("Ticket");
    state.flush();
    assert.equal(state.submissions.length, 1);
    assert.doesNotThrow(() => setup({withForm: false}));
});

test("an automatic search restores focus and the cursor on the resulting page", () => {
    const state = setup();
    state.search.focus();
    state.type("Wasserhahn");
    state.search.setSelectionRange(6, 6);
    state.flush();
    const savedFocus = JSON.parse(state.storage.get("ticketSearchFocus"));
    const nextPage = setup({savedFocus, searchText: "Wasserhahn"});
    assert.equal(nextPage.document.activeElement, nextPage.search);
    assert.equal(nextPage.search.selectionStart, 6);
    assert.equal(nextPage.search.selectionEnd, 6);
    assert.equal(nextPage.storage.size, 0);
    const otherSearch = setup({savedFocus, searchText: "Anderer Begriff"});
    assert.notEqual(otherSearch.document.activeElement, otherSearch.search);
});

test("automatic search still submits when focus storage is unavailable", () => {
    const state = setup({storageUnavailable: true});
    state.search.focus();
    state.type("Ticket");
    assert.doesNotThrow(() => state.flush());
    assert.equal(state.submissions.length, 1);
});

test("returning to the bare ticket list restores all filters, multiple owners, search and page", () => {
    const query = "owner=erp%3A11&owner=erp%3A12&state=ignored&priority=3&department=7&ai_state=ready&q=Wasserhahn&page=2";
    const first = setup({persistence: true, currentUrl: "https://example.invalid/admin/tickets?" + query, canonicalUrl: "/admin/tickets?" + query});
    assert.equal(first.navigations.length, 0);
    const returned = setup({persistence: true, storageEntries: first.storage});
    assert.equal(returned.navigations.length, 1);
    const url = new URL(returned.navigations[0]);
    assert.equal(url.pathname, "/admin/tickets");
    assert.deepEqual(url.searchParams.getAll("owner"), ["erp:11", "erp:12"]);
    for (const [name, value] of new URLSearchParams(query)) {
        if (name !== "owner") assert.equal(url.searchParams.get(name), value);
    }
});

test("explicit filter links replace stored choices and reloads do not loop", () => {
    const stored = [["ticket-filters-1:/admin/tickets", JSON.stringify({query: "owner=all&state=all&q=Alt&page=4"})]];
    const explicit = setup({persistence: true, storageEntries: stored, currentUrl: "https://example.invalid/admin/tickets?owner=mine&state=open"});
    assert.equal(explicit.navigations.length, 0);
    assert.match(JSON.parse(explicit.storage.get("ticket-filters-1:/admin/tickets")).query, /owner=mine/);
    const next = setup({persistence: true, storageEntries: explicit.storage});
    assert.equal(next.navigations.length, 0);
});

test("reset removes stored filters and search focus before saving the defaults", () => {
    const state = setup({persistence: true, currentUrl: "https://example.invalid/admin/tickets?reset=1",
        storageEntries: [["ticket-filters-1:/admin/tickets", JSON.stringify({query: "owner=all&state=ignored&q=Alt"})]],
        savedFocus: {value: "Alt", start: 1, end: 2}});
    assert.equal(state.navigations.length, 0);
    assert.deepEqual(state.history, ["/admin/tickets"]);
    assert.equal(state.storage.has("ticketSearchFocus"), false);
    assert.equal(new URLSearchParams(JSON.parse(state.storage.get("ticket-filters-1:/admin/tickets")).query).get("state"), "open");
    const returned = setup({persistence: true, storageEntries: state.storage});
    assert.equal(returned.navigations.length, 0);
});

test("ticket filter memory is scoped to the current user and tab", () => {
    const first = setup({persistence: true, currentUrl: "https://example.invalid/admin/tickets?owner=all", canonicalUrl: "/admin/tickets?owner=all&state=all"});
    const otherUser = setup({persistence: true, storageEntries: first.storage, stateKey: "ticket-filters-2"});
    assert.equal(otherUser.navigations.length, 0);
    assert.equal(setup({persistence: true}).navigations.length, 0);
    assert.equal(setup({persistence: true, storageEntries: first.storage}).navigations.length, 1);
});

test("malformed or unrelated stored data is replaced without following it", () => {
    for (const saved of ['{broken', 'null', JSON.stringify({query: "next=https%3A%2F%2Fexample.invalid"}), JSON.stringify({query: ["owner=all"]})]) {
        const state = setup({persistence: true, storageEntries: [["ticket-filters-1:/admin/tickets", saved]]});
        assert.equal(state.navigations.length, 0);
        assert.match(JSON.parse(state.storage.get("ticket-filters-1:/admin/tickets")).query, /owner=mine/);
    }
});

test("unavailable storage still allows normal automatic filtering", () => {
    const state = setup({persistence: true, storageUnavailable: true});
    state.type("Reparatur");
    state.flush();
    assert.equal(state.submissions.length, 1);
    assert.equal(state.navigations.length, 0);
});

test("reset cancels a pending search before navigation", () => {
    const state = setup();
    state.type("Noch nicht abgesendet");
    state.reset.listeners.click();
    state.flush();
    assert.equal(state.submissions.length, 0);
});

test("ticket actions cannot be interrupted by a pending automatic search", () => {
    const state = setup();
    state.type("Noch nicht abgesendet");
    state.document.listeners.submit({target: {method: "post"}});
    state.flush();
    assert.equal(state.submissions.length, 0);
});

