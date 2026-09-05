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
    path.join(__dirname, '../templates/settings/data.html'), 'utf8'
);
const exportForm = template.match(
    /<form action="\/settings\/portability-v2\/export"[\s\S]*?<\/form>/
)[0];
const inputHandler = exportForm.match(/@input="(\w+)\(\)"/)[1];

function setup(runtime = {}) {
    const timers = [];
    const window = {
        setTimeout(callback, delay) { timers.push({ callback, delay }); },
        location: { href: '' },
        vitalsConfirm: async () => true
    };
    vm.runInNewContext(source, { window, ...runtime });
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

    return { component, passphrase, confirmation, timers, submit, window };
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

for (const action of ['inspect', 'apply']) {
    for (const target of [
        '/auth/start?step_up=true&next=%2Fsettings%2Fdata',
        '/login?next=%2Fsettings%2Fdata',
        'https://untrusted.example/login',
        '//untrusted.example/login',
        '/auth/start/elsewhere',
        null
    ]) {
        test(`${action} returns to Data through a safe reauthentication target: ${target}`, async () => {
            const calls = [];
            const form = setup({
                FormData: class { set() {} },
                fetch: async (url, options) => {
                    calls.push({ url, options });
                    return {
                        status: 401,
                        ok: false,
                        headers: { get(name) {
                            assert.equal(name, 'X-Vitals-Reauthentication');
                            return target;
                        } },
                        json: async () => ({ detail: 'Recent authentication required' })
                    };
                }
            });
            let reset = false;
            form.component.$refs.importForm = {
                reportValidity: () => true,
                reset() { reset = true; }
            };
            form.component.inspection = { operation_id: 'synthetic-operation', connections: [] };
            await form.component[action]();
            const expected = target && (
                target.startsWith('/auth/start?') || target.startsWith('/login?')
            ) ? target : '/login?next=%2Fsettings%2Fdata';
            assert.equal(form.window.location.href, expected);
            assert.equal(calls.length, 1);
            assert.equal(calls[0].url, `/settings/portability-v2/${action}`);
            assert.equal(calls[0].options.credentials, 'same-origin');
            assert.equal(form.component.busy, '');
            assert.equal(form.component.error, '');
            assert.equal(reset, false);
            assert.equal(form.component.result, null);
        });
    }
}
