const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../app/static/js/document_finalization.js"), "utf8");

function fixture(confirmation = true) {
  const listeners = [];
  const confirmations = [];
  const tasks = [];
  const form = {addEventListener: (type, handler) => listeners.push(handler)};
  const button = {form, disabled: false, textContent: "Festschreiben", dataset: {confirmMessage: "Aktuelle Eingaben festschreiben?", pendingText: "Wird festgeschrieben …"}};
  vm.runInNewContext(source, {
    document: {addEventListener: (type, handler) => handler(), querySelectorAll: () => [button]},
    window: {confirm: message => { confirmations.push(message); return confirmation; }},
    setTimeout: handler => tasks.push(handler)
  });
  const submit = (submitter = button, prevented = false) => {
    const event = {submitter, defaultPrevented: prevented, preventDefault() { this.defaultPrevented = true; }};
    listeners.forEach(handler => handler(event));
    return event;
  };
  return {button, confirmations, submit, flush: () => { while (tasks.length) tasks.shift()(); }};
}

test("finalization confirms once and submits the native form without replacing its data", () => {
  const state = fixture();
  const event = state.submit();
  assert.equal(event.defaultPrevented, false);
  assert.deepEqual(state.confirmations, ["Aktuelle Eingaben festschreiben?"]);
  state.flush();
  assert.equal(state.button.disabled, true);
  assert.equal(state.button.textContent, "Wird festgeschrieben …");
  assert.equal(state.submit().defaultPrevented, true);
  assert.equal(state.confirmations.length, 1);
});

test("saving a draft or pressing Enter does not finalize or ask for confirmation", () => {
  const state = fixture();
  for (const submitter of [{}, null]) {
    assert.equal(state.submit(submitter).defaultPrevented, false);
  }
  state.flush();
  assert.equal(state.button.disabled, false);
  assert.deepEqual(state.confirmations, []);
});

test("canceling the confirmation keeps the edited form available", () => {
  const state = fixture(false);
  assert.equal(state.submit().defaultPrevented, true);
  state.flush();
  assert.equal(state.button.disabled, false);
  assert.equal(state.button.textContent, "Festschreiben");
});

test("pending recipient or payment reads do not leave finalization disabled", () => {
  const state = fixture();
  assert.equal(state.submit(state.button, true).defaultPrevented, true);
  state.flush();
  assert.equal(state.button.disabled, false);
  assert.deepEqual(state.confirmations, []);
  const event = state.submit();
  event.preventDefault();
  state.flush();
  assert.equal(state.button.disabled, false);
  assert.equal(state.submit().defaultPrevented, false);
});
