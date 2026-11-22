const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

test('changing the decision keeps the configured labels and multiline instructions', () => {
  const label = { textContent: '' };
  const intro = { textContent: '' };
  let changed;
  const form = {
    elements: { decision: { value: 'accept' } },
    dataset: {
      acceptLabel: 'Eigene Mitteilung (freiwillig)', rejectLabel: 'Eigener Absagegrund (freiwillig)',
      acceptIntro: 'Guten Tag Firma Beispiel!\nBitte Zusage bestätigen.', rejectIntro: 'Bitte Absage bestätigen.\nEine Angabe ist freiwillig.',
    },
    addEventListener: (event, handler) => { if (event === 'change') changed = handler; },
  };
  const elements = { orderResponseForm: form, responseTextLabel: label, responseIntro: intro };
  const source = fs.readFileSync(path.join(__dirname, '../app/static/js/external_order_response.js'), 'utf8');
  vm.runInNewContext(source, { document: { getElementById: id => elements[id] } });
  assert.equal(label.textContent, form.dataset.acceptLabel);
  assert.equal(intro.textContent, form.dataset.acceptIntro);
  form.elements.decision.value = 'reject';
  changed();
  assert.equal(label.textContent, form.dataset.rejectLabel);
  assert.equal(intro.textContent, form.dataset.rejectIntro);
  form.elements.decision.value = 'accept';
  changed();
  assert.equal(label.textContent, form.dataset.acceptLabel);
});
