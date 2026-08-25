/* Explicit, current-device-only Web Push enrollment.

   Reading capability and an existing local subscription is harmless. The one
   browser action that may display a permission prompt lives only in the enable
   button handler; page load never asks for notification permission. */
(function () {
  'use strict';

  const ROOT = '/account/notifications';

  function setState(card, name, options) {
    const status = card.querySelector('[data-web-push-status]');
    const enable = card.querySelector('[data-web-push-enable]');
    const disable = card.querySelector('[data-web-push-disable]');
    const settings = options || {};
    status.textContent = card.dataset['status' + name[0].toUpperCase() + name.slice(1)];
    // `.v-btn-ghost` is defined after Tailwind and sets `display:inline-flex`,
    // so a utility `hidden` class loses in the cascade. An element-level display
    // value is the unambiguous state boundary for these two controls.
    enable.style.display = settings.enable ? '' : 'none';
    disable.style.display = settings.disable ? '' : 'none';
    enable.disabled = Boolean(settings.busy);
    disable.disabled = Boolean(settings.busy);
  }

  async function api(path, options) {
    const response = await fetch(ROOT + path, Object.assign({
      credentials: 'same-origin',
      headers: { 'Accept': 'application/json' }
    }, options || {}));
    let body = {};
    try { body = await response.json(); } catch (_error) { /* generic below */ }
    if (!response.ok) {
      const error = new Error('notification request failed');
      error.status = response.status;
      error.detail = body.detail;
      throw error;
    }
    return body;
  }

  function jsonPost(path, body) {
    return api(path, {
      method: 'POST',
      headers: {
        'Accept': 'application/json',
        'Content-Type': 'application/json'
      },
      body: JSON.stringify(body)
    });
  }

  function decodeApplicationServerKey(value) {
    const padding = '='.repeat((4 - value.length % 4) % 4);
    const raw = atob((value + padding).replace(/-/g, '+').replace(/_/g, '/'));
    return Uint8Array.from(raw, function (character) { return character.charCodeAt(0); });
  }

  function sameApplicationServerKey(subscription, configuredKey) {
    const actual = subscription.options && subscription.options.applicationServerKey;
    if (!actual) return false;
    const expected = decodeApplicationServerKey(configuredKey);
    const bytes = new Uint8Array(actual);
    if (bytes.length !== expected.length) return false;
    return bytes.every(function (value, index) { return value === expected[index]; });
  }

  async function rootRegistration() {
    let registration = await navigator.serviceWorker.getRegistration('/');
    if (!registration) {
      registration = await navigator.serviceWorker.register('/sw.js', {
        scope: '/',
        updateViaCache: 'none'
      });
    }
    return registration;
  }

  async function rememberOwnedLocale(registration) {
    try {
      const ready = registration.active
        ? registration
        : await navigator.serviceWorker.ready;
      if (!ready.active) return;
      ready.active.postMessage({
        kind: 'set_locale',
        v: 1,
        locale: document.documentElement.lang === 'ru' ? 'ru' : 'en'
      });
    } catch (_error) {
      // Enrollment is authoritative; locale sync is a best-effort preference.
    }
  }

  async function inspect(card, config) {
    const registration = await navigator.serviceWorker.getRegistration('/');
    const subscription = registration
      ? await registration.pushManager.getSubscription()
      : null;
    if (!subscription) {
      if (Notification.permission === 'denied') {
        setState(card, 'denied');
      } else {
        setState(card, 'ready', { enable: true });
      }
      return;
    }
    const serverState = await jsonPost('/status', { endpoint: subscription.endpoint });
    if (serverState.enabled) {
      await rememberOwnedLocale(registration);
      if (Notification.permission === 'denied') {
        setState(card, 'denied', { disable: true });
      } else if (sameApplicationServerKey(subscription, config.applicationServerKey)) {
        setState(card, 'enabled', { disable: true });
      } else {
        card.dataset.webPushOwnStale = '1';
        setState(card, 'stale', { enable: true, disable: true });
      }
      return;
    }
    // It may be a revoked row or another signed-in account in a shared browser.
    // The server intentionally does not distinguish those cases.
    setState(card, 'conflict', { enable: true });
  }

  async function enable(card, config) {
    setState(card, 'checking', { busy: true });
    const permission = await Notification.requestPermission();
    if (permission !== 'granted') {
      setState(card, 'denied');
      return;
    }
    const registration = await rootRegistration();
    let subscription = await registration.pushManager.getSubscription();
    if (subscription && card.dataset.webPushOwnStale === '1') {
      // This endpoint was confirmed as belonging to the current account. The
      // explicit reconnect click authorizes replacing it after VAPID rotation.
      await jsonPost('/subscription/revoke', { endpoint: subscription.endpoint });
      await subscription.unsubscribe();
      subscription = null;
      delete card.dataset.webPushOwnStale;
    }
    subscription = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: decodeApplicationServerKey(config.applicationServerKey)
    });
    const payload = subscription.toJSON();
    await jsonPost('/subscription', {
      endpoint: subscription.endpoint,
      keys: payload.keys
    });
    await rememberOwnedLocale(registration);
    setState(card, 'enabled', { disable: true });
  }

  async function disable(card) {
    setState(card, 'checking', { busy: true });
    const registration = await navigator.serviceWorker.getRegistration('/');
    const subscription = registration
      ? await registration.pushManager.getSubscription()
      : null;
    if (subscription) {
      await jsonPost('/subscription/revoke', { endpoint: subscription.endpoint });
      await subscription.unsubscribe();
    }
    delete card.dataset.webPushOwnStale;
    setState(card, 'ready', { enable: true });
  }

  function showError(card, error) {
    if (error && error.detail === 'device_linked_elsewhere') {
      setState(card, 'conflict', { enable: true });
    } else if (error && error.detail === 'device_limit_reached') {
      setState(card, 'limit');
    } else if (error && error.detail === 'notifications_unavailable') {
      setState(card, 'unavailable');
    } else {
      setState(card, 'error', { enable: true });
    }
  }

  async function init(card) {
    if (card.dataset.webPushReady) return;
    card.dataset.webPushReady = '1';
    if (!('Notification' in window) || !('PushManager' in window)
        || !('serviceWorker' in navigator)) {
      setState(card, 'unsupported');
      return;
    }
    try {
      const config = await api('/configuration');
      if (!config.available || !config.applicationServerKey) {
        setState(card, 'unavailable');
        return;
      }
      card.querySelector('[data-web-push-enable]').addEventListener('click', function () {
        enable(card, config).catch(function (error) { showError(card, error); });
      });
      card.querySelector('[data-web-push-disable]').addEventListener('click', function () {
        disable(card).catch(function (error) { showError(card, error); });
      });
      await inspect(card, config);
    } catch (error) {
      showError(card, error);
    }
  }

  function initAll(root) {
    (root || document).querySelectorAll('[data-web-push-card]').forEach(init);
  }

  document.addEventListener('DOMContentLoaded', function () { initAll(document); });
  document.addEventListener('htmx:load', function (event) { initAll(event.target); });
})();
