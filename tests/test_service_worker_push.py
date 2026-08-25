"""Executable contracts for PHI-free care notifications in the PWA worker."""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node.js is required to execute the worker")
def test_worker_accepts_only_generic_wakeup_and_opens_fixed_inbox():
    worker = (ROOT / "web/static/sw.js").read_text(encoding="utf-8")
    harness = textwrap.dedent(
        r"""
        const fs = require('node:fs');
        const vm = require('node:vm');
        const source = fs.readFileSync(0, 'utf8');
        const listeners = {};
        const shown = [];
        const opened = [];
        let openedFocuses = 0;
        let windowClients = [];
        const cacheRows = new Map();
        const deletedCaches = [];

        class FakeResponse {
          constructor(body) { this.body = String(body); }
          async text() { return this.body; }
        }

        function cacheFor(name) {
          if (!cacheRows.has(name)) cacheRows.set(name, new Map());
          const rows = cacheRows.get(name);
          return {
            async add() {},
            async match(key) { return rows.get(String(key)); },
            async put(key, value) { rows.set(String(key), value); }
          };
        }
        const selfObject = {
          __VITALS_PUSH_COPY__: {
            en: { title: 'New message', body: 'Open inbox.' },
            ru: { title: 'Новое сообщение', body: 'Откройте входящие.' }
          },
          navigator: { language: 'en-US' },
          location: { origin: 'https://vitals.test' },
          registration: {
            async showNotification(title, options) { shown.push({ title, options }); }
          },
          clients: {
            async claim() {},
            async matchAll() { return windowClients; },
            async openWindow(url) {
              opened.push(url);
              return { async focus() { openedFocuses += 1; } };
            }
          },
          addEventListener(name, listener) { listeners[name] = listener; },
          skipWaiting() {}
        };
        const sandbox = {
          URL,
          Response: FakeResponse,
          caches: {
            async open(name) { return cacheFor(name); },
            async keys() { return ['vitals-os-v8', 'vitals-sw-prefs-v1']; },
            async delete(name) { deletedCaches.push(name); return true; }
          },
          fetch: async () => ({ status: 200, clone() { return this; } }),
          self: selfObject
        };
        vm.runInNewContext(source, sandbox, { filename: 'sw.js' });

        async function emit(name, event) {
          let pending = Promise.resolve();
          event.waitUntil = (value) => { pending = Promise.resolve(value); };
          listeners[name](event);
          await pending;
        }
        function pushData(value) {
          return { data: { json() { return value; } } };
        }

        (async () => {
          await emit('push', pushData({ kind: 'care_message', v: 1 }));
          if (shown.length !== 1 || shown[0].title !== 'New message') throw Error('valid EN wakeup');
          const notification = shown[0].options;
          if (notification.tag !== 'vitals-care-message') throw Error('stable coalescing tag');
          if (JSON.stringify(notification.data) !== JSON.stringify({ kind: 'care_message', v: 1 })) throw Error('generic notification data');
          if ('url' in notification.data || 'body' in notification.data) throw Error('PHI-bearing data');

          await emit('push', pushData({ kind: 'care_message', v: 1, url: '/care/patient/secret' }));
          await emit('push', pushData({ kind: 'other', v: 1 }));
          await emit('push', pushData(null));
          await emit('push', pushData([]));
          await emit('push', pushData({ kind: 'care_message' }));
          await emit('push', pushData({ kind: 'care_message', v: 2 }));
          await emit('push', { data: { json() { throw Error('bad JSON'); } } });
          if (shown.length !== 1) throw Error('malformed payload was rendered');

          await emit('message', {
            data: { kind: 'set_locale', v: 1, locale: 'ru' },
            source: { type: 'window', url: 'https://vitals.test/messages' }
          });
          await emit('push', pushData({ kind: 'care_message', v: 1 }));
          if (shown.length !== 2 || shown[1].title !== 'Новое сообщение') throw Error('stored RU locale');

          await emit('message', {
            data: { kind: 'set_locale', v: 1, locale: 'en' },
            source: { type: 'window', url: 'https://attacker.test/' }
          });
          await emit('message', {
            data: { kind: 'set_locale', v: 1, locale: 'en', extra: true },
            source: { type: 'window', url: 'https://vitals.test/messages' }
          });
          await emit('message', {
            data: { kind: 'set_locale', v: 1, locale: 'en' },
            source: { type: 'worker', url: 'https://vitals.test/sw-helper.js' }
          });
          await emit('push', pushData({ kind: 'care_message', v: 1 }));
          if (shown[2].title !== 'Новое сообщение') throw Error('cross-origin locale overwrite');

          await emit('activate', {});
          if (JSON.stringify(deletedCaches) !== JSON.stringify(['vitals-os-v8'])) throw Error('preference cache lifecycle');

          let forgedClosed = false;
          await emit('notificationclick', {
            notification: {
              data: { kind: 'care_message', v: 1, url: '/care/patient/secret' },
              close() { forgedClosed = true; }
            }
          });
          if (!forgedClosed || opened.length !== 0) throw Error('forged click data');

          await emit('notificationclick', {
            notification: {
              data: { kind: 'care_message', v: 1 },
              close() {}
            }
          });
          if (JSON.stringify(opened) !== JSON.stringify(['/messages']) || openedFocuses !== 1) throw Error('fixed inbox target');

          let exactFocuses = 0;
          windowClients = [{
            url: 'https://vitals.test/messages',
            async focus() { exactFocuses += 1; }
          }];
          await emit('notificationclick', {
            notification: { data: { kind: 'care_message', v: 1 }, close() {} }
          });
          if (exactFocuses !== 1 || opened.length !== 1) throw Error('exact client focus');

          windowClients = [{
            url: 'https://vitals.test/messages',
            async focus() { throw Error('closed exact client'); }
          }];
          await emit('notificationclick', {
            notification: { data: { kind: 'care_message', v: 1 }, close() {} }
          });
          if (opened.length !== 2) throw Error('exact focus fallback');

          windowClients = [{
            url: 'https://vitals.test/today',
            async navigate() {
              return { async focus() { throw Error('closed navigated client'); } };
            }
          }];
          await emit('notificationclick', {
            notification: { data: { kind: 'care_message', v: 1 }, close() {} }
          });
          if (opened.length !== 3) throw Error('navigated focus fallback');

          windowClients = [{
            url: 'https://vitals.test/today',
            async navigate() { throw Error('navigation rejected'); }
          }];
          await emit('notificationclick', {
            notification: { data: { kind: 'care_message', v: 1 }, close() {} }
          });
          if (opened.length !== 4 || opened.some(url => url !== '/messages')) throw Error('navigate fallback target');
        })().catch((error) => {
          console.error(error.stack || error);
          process.exitCode = 1;
        });
        """
    )
    result = subprocess.run(
        [NODE, "-e", harness],
        input=worker,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
