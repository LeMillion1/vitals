'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(
    path.join(__dirname, '../static/portability_v2.js'), 'utf8'
);
const template = fs.readFileSync(
    path.join(__dirname, '../templates/settings/settings.html'), 'utf8'
);
const exportForm = template.match(
    /<form action="\/settings\/portability-v2\/export"[\s\S]*?<\/form>/
)[0];
const inputHandler = exportForm.match(/@input="(\w+)\(\)"/)[1];

function setup() {
    const timers = [];
    const window = {
        setTimeout(callback, delay) { timers.push({ callback, delay }); }
    };
    vm.runInNewContext(source, { window });
    const component = window.portabilityV2();
    const passphrase = { value: 'synthetic-export-passphrase' };
    const confirmation = {
        value: 'different-synthetic-passphrase',
        validationMessage: '',
        reports: 0,
        setCustomValidity(message) { this.validationMessage = message; },
        reportValidity() { this.reports += 1; }
    };
    component.$refs = {
        exportPassphrase: passphrase,
        exportPassphraseConfirmation: confirmation
    };

    function submit() {
        // Browsers suppress submit while a custom error is still present.
        if (confirmation.validationMessage) return false;
        let prevented = false;
        component.prepareExport({ preventDefault() { prevented = true; } });
        return !prevented;
    }

    return { component, passphrase, confirmation, timers, submit };
}

for (const correctedField of ['confirmation', 'passphrase']) {
    test(`a mismatch can be corrected by editing ${correctedField}`, () => {
        const form = setup();
        assert.equal(form.submit(), false);
        assert.equal(form.confirmation.reports, 1);
        assert.notEqual(form.confirmation.validationMessage, '');
        assert.equal(form.timers.length, 0);

        if (correctedField === 'confirmation') {
            form.confirmation.value = form.passphrase.value;
        } else {
            form.passphrase.value = form.confirmation.value;
        }
        // Use the handler actually wired to bubbling input events in the form.
        form.component[inputHandler]();
        assert.equal(form.confirmation.validationMessage, '');
        assert.equal(form.submit(), true);
        assert.notEqual(form.passphrase.value, '');
        assert.equal(form.timers.length, 1);
        assert.equal(form.timers[0].delay, 1000);
        form.timers[0].callback();
        assert.equal(form.passphrase.value, '');
        assert.equal(form.confirmation.value, '');
    });
}

test('editing does not allow a remaining mismatch to be exported', () => {
    const form = setup();
    assert.equal(form.submit(), false);
    form.confirmation.value = 'still-not-the-same-passphrase';
    form.component[inputHandler]();
    assert.equal(form.submit(), false);
    assert.equal(form.confirmation.reports, 2);
    assert.notEqual(form.confirmation.validationMessage, '');
    assert.equal(form.timers.length, 0);
});
