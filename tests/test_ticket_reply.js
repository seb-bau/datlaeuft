const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../app/static/js/ticket_reply.js"), "utf8");

function replyForm(fetch) {
    const elements = {};
    for (const id of ["ticketReplyModal", "ticketReplyRecipient", "ticketReplyRecipients", "ticketReplyText", "ticketReplyStatus", "ticketReplyTitle"]) {
        elements[id] = {
            value: "", textContent: "", children: [], listeners: {},
            addEventListener(name, handler) { this.listeners[name] = handler; },
            replaceChildren() { this.children = []; },
            appendChild(child) { this.children.push(child); },
        };
    }
    const form = {action: ""};
    elements.ticketReplyModal.querySelector = () => form;
    vm.runInNewContext(source, {
        document: {
            addEventListener: (name, handler) => handler(),
            getElementById: id => elements[id], createElement: () => ({}),
        }, fetch,
    });
    return {elements, form, open: () => elements.ticketReplyModal.listeners["show.bs.modal"]({relatedTarget: {dataset: {
        replyUrl: "/tickets/1/reply", recipientsUrl: "/tickets/1/recipients", ticketNumber: "12345",
    }}})};
}

test("the reply dialog displays the ticket and one suggested email recipient", async () => {
    const state = replyForm(async () => ({ok: true, json: async () => ({recipients: ["tenant@example.invalid"]})}));
    await state.open();
    assert.equal(state.form.action, "/tickets/1/reply");
    assert.equal(state.elements.ticketReplyRecipient.value, "tenant@example.invalid");
    assert.match(state.elements.ticketReplyTitle.textContent, /12345/);
});

test("multiple or missing recipients require an explicit choice", async () => {
    for (const recipients of [[], ["one@example.invalid", "two@example.invalid"]]) {
        const state = replyForm(async () => ({ok: true, json: async () => ({recipients})}));
        await state.open();
        assert.equal(state.elements.ticketReplyRecipient.value, "");
        assert.equal(state.elements.ticketReplyRecipients.children.length, recipients.length);
    }
});

test("a late lookup cannot replace a typed recipient or populate a closed dialog", async () => {
    let resolve;
    const pending = new Promise(done => { resolve = done; });
    const state = replyForm(() => pending);
    const opening = state.open();
    state.elements.ticketReplyRecipient.value = "chosen@example.invalid";
    resolve({ok: true, json: async () => ({recipients: ["suggested@example.invalid"]})});
    await opening;
    assert.equal(state.elements.ticketReplyRecipient.value, "chosen@example.invalid");

    let resolveSecond;
    const second = replyForm(() => new Promise(done => { resolveSecond = done; }));
    const nextOpening = second.open();
    second.elements.ticketReplyModal.listeners["hidden.bs.modal"]();
    resolveSecond({ok: true, json: async () => ({recipients: ["stale@example.invalid"]})});
    await nextOpening;
    assert.equal(second.elements.ticketReplyRecipient.value, "");
});

test("lookup errors are shown without preventing manual entry", async () => {
    const state = replyForm(async () => ({ok: false, json: async () => ({message: "Bitte Empfänger prüfen."})}));
    await state.open();
    assert.equal(state.elements.ticketReplyStatus.textContent, "Bitte Empfänger prüfen.");
    assert.equal(state.elements.ticketReplyRecipient.value, "");
});
