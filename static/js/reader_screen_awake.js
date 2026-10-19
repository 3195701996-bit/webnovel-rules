(function () {
  'use strict';
  // Progressive enhancement: denial/unsupported contexts never block reading.
  if (!window.isSecureContext || !navigator.wakeLock ||
      typeof navigator.wakeLock.request !== 'function') return;
  let sentinel = null, pending = false, suspended = false, generation = 0;
  const visible = () => !suspended && document.visibilityState === 'visible';
  function release(lock) {
    if (!lock) return;
    try { Promise.resolve(lock.release()).catch(() => {}); } catch (_) {}
  }
  function relinquish() {
    generation++;
    const previous = sentinel;
    sentinel = null;
    release(previous);
  }
  async function acquire() {
    if (!visible() || pending || sentinel) return;
    const ticket = generation;
    pending = true;
    try {
      const lock = await navigator.wakeLock.request('screen');
      if (ticket !== generation || !visible()) {
        release(lock);
        return;
      }
      sentinel = lock;
      lock.addEventListener('release', () => {
        if (sentinel === lock) sentinel = null;
        // Respect OS/battery/user revocation: no immediate reacquisition loop.
      }, {once:true});
    } catch (_) {
      // Permission policy, low power mode, HTTP LAN and unsupported browsers
      // must retain the same usable reader and progress-saving behavior.
    } finally {
      pending = false;
      // A request may finish after hide->show; discard its stale lease first.
      if (ticket !== generation && visible()) acquire();
    }
  }
  document.addEventListener('visibilitychange', () => {
    if (visible()) acquire(); else relinquish();
  });
  window.addEventListener('pagehide', () => {
    suspended = true;
    relinquish();
  });
  window.addEventListener('pageshow', () => {
    suspended = false;
    acquire();
  });
  acquire();
}());
