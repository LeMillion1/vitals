/* Encrypted personal-record workflow for Settings.

   The selected file and passphrase stay in this page: inspection and apply each
   upload the same browser-held bytes, and the server never persists an
   inspection token or plaintext archive between those two decisions. */
(function () {
    'use strict';

    function translated(key, fallback) {
        if (typeof window.t !== 'function') return fallback;
        var value = window.t(key);
        return value === key ? fallback : value;
    }

    async function responsePayload(response) {
        try {
            return await response.json();
        } catch (_error) {
            return {};
        }
    }

    window.portabilityV2 = function () {
        return {
            busy: '',
            error: '',
            inspection: null,
            mapping: {},
            result: null,

            clearExportError: function () {
                // Native validation runs before submit. Editing either field
                // must release a prior mismatch so prepareExport can run again.
                this.$refs.exportPassphraseConfirmation.setCustomValidity('');
            },

            prepareExport: function (event) {
                var passphrase = this.$refs.exportPassphrase;
                var confirmation = this.$refs.exportPassphraseConfirmation;
                this.clearExportError();
                if (passphrase.value !== confirmation.value) {
                    event.preventDefault();
                    confirmation.setCustomValidity(translated(
                        'portability.passphrase_mismatch',
                        'The passphrases do not match.'
                    ));
                    confirmation.reportValidity();
                    return;
                }
                window.setTimeout(function () {
                    passphrase.value = '';
                    confirmation.value = '';
                }, 1000);
            },

            inspect: async function () {
                var form = this.$refs.importForm;
                if (!form.reportValidity()) return;
                this.busy = 'inspect';
                this.error = '';
                this.result = null;
                this.inspection = null;
                this.mapping = {};
                try {
                    var response = await fetch('/settings/portability-v2/inspect', {
                        method: 'POST',
                        body: new FormData(form),
                        credentials: 'same-origin',
                        headers: { 'Accept': 'application/json' }
                    });
                    var payload = await responsePayload(response);
                    if (response.status === 401) {
                        window.location.href = '/login?next=%2Fsettings';
                        return;
                    }
                    if (!response.ok) throw new Error(payload.detail || translated(
                        'portability.inspect_failed',
                        'The file could not be checked.'
                    ));
                    var chosen = {};
                    payload.connections.forEach(function (connection) {
                        if (connection.candidates.length === 1) {
                            chosen[connection.ref] = connection.candidates[0].id;
                        }
                    });
                    this.mapping = chosen;
                    this.inspection = payload;
                } catch (error) {
                    this.error = error && error.message ? error.message : translated(
                        'portability.inspect_failed',
                        'The file could not be checked.'
                    );
                } finally {
                    this.busy = '';
                }
            },

            canApply: function () {
                if (!this.inspection || this.busy) return false;
                return this.inspection.connections.every(function (connection) {
                    return connection.candidates.some(function (candidate) {
                        return candidate.id === this.mapping[connection.ref];
                    }, this);
                }, this);
            },

            apply: async function () {
                if (!this.canApply()) return;
                var approved = await window.vitalsConfirm(
                    translated(
                        'portability.replace_confirm',
                        'Replace your personal record with this protected file?'
                    ),
                    translated('portability.replace_action', 'Replace record')
                );
                if (!approved) return;

                this.busy = 'apply';
                this.error = '';
                try {
                    var form = this.$refs.importForm;
                    var body = new FormData(form);
                    body.set('operation_id', this.inspection.operation_id);
                    body.set('connection_mapping', JSON.stringify(this.mapping));
                    body.set('confirmation', 'replace');
                    var response = await fetch('/settings/portability-v2/apply', {
                        method: 'POST',
                        body: body,
                        credentials: 'same-origin',
                        headers: { 'Accept': 'application/json' }
                    });
                    var payload = await responsePayload(response);
                    if (response.status === 401) {
                        window.location.href = '/login?next=%2Fsettings';
                        return;
                    }
                    if (!response.ok) throw new Error(payload.detail || translated(
                        'portability.apply_failed',
                        'The record could not be restored.'
                    ));
                    this.result = payload;
                    this.inspection = null;
                    this.mapping = {};
                    form.reset();
                } catch (error) {
                    this.error = error && error.message ? error.message : translated(
                        'portability.apply_failed',
                        'The record could not be restored.'
                    );
                } finally {
                    this.busy = '';
                }
            }
        };
    };
})();
