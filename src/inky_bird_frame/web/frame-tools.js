// Shared viewing tools. No session renewal, source changes or panel telemetry.
export function frameTools({ viewport, controls, status, button, previous, next, togglePlay }) {
  let lock = null,
    keepAwake = false,
    quietTimer = null,
    startX = null,
    disposed = false,
    acquiring = false,
    generation = 0;
  const zoom = button('Zoom', () => {
    viewport.classList.toggle('zoomed');
    zoom.textContent = viewport.classList.contains('zoomed') ? 'Fit whole plate' : 'Zoom';
  });
  controls.append(zoom);
  if (document.documentElement.requestFullscreen)
    controls.append(
      button('Fullscreen', async () => {
        try {
          if (document.fullscreenElement) await document.exitFullscreen();
          else await document.documentElement.requestFullscreen();
        } catch {
          status.textContent = 'Fullscreen is not available here. Viewing still works.';
        }
      }),
    );
  let awake;
  function releaseLock() {
    const held = lock;
    lock = null;
    return held?.release().catch(() => {
      status.textContent =
        'The browser could not confirm screen-sleep release. Close this view to stop keeping it awake.';
    });
  }
  async function acquire() {
    if (lock || disposed || acquiring) return;
    acquiring = true;
    const requested = generation;
    try {
      const acquired = await navigator.wakeLock.request('screen');
      if (!keepAwake || disposed || document.hidden || requested !== generation) {
        await acquired.release();
        return;
      }
      lock = acquired;
      acquired.addEventListener('release', () => {
        if (lock === acquired) lock = null;
      });
    } catch {
      if (requested === generation) {
        keepAwake = false;
        if (awake) awake.textContent = 'Keep screen awake';
        status.textContent =
          'This browser could not keep the screen awake. Its normal sleep settings apply.';
      }
    } finally {
      acquiring = false;
      if (keepAwake && !lock && !disposed && !document.hidden) acquire();
    }
  }
  if (navigator.wakeLock && window.isSecureContext) {
    awake = button('Keep screen awake', async () => {
      keepAwake = !keepAwake;
      if (keepAwake) await acquire();
      else {
        generation += 1;
        await releaseLock();
      }
      awake.textContent = keepAwake ? 'Allow screen sleep' : 'Keep screen awake';
    });
    controls.append(awake);
  }
  const visible = () => {
    if (document.hidden) {
      generation += 1;
      releaseLock();
    } else if (keepAwake) acquire();
  };
  const key = (event) => {
    if (event.target.closest('input,textarea,select,button,a,[contenteditable]')) return;
    if (event.key === 'ArrowRight') next();
    if (event.key === 'ArrowLeft') previous();
    if (event.key === ' ') {
      event.preventDefault();
      togglePlay();
    }
  };
  const down = (event) => {
    startX = event.isPrimary && !viewport.classList.contains('zoomed') ? event.clientX : null;
  };
  const cancel = () => {
    startX = null;
  };
  const up = (event) => {
    if (startX !== null && Math.abs(event.clientX - startX) > 60) {
      if (event.clientX < startX) next();
      else previous();
    }
    startX = null;
  };
  const reveal = () => {
    controls.classList.remove('quiet');
    clearTimeout(quietTimer);
    quietTimer = setTimeout(() => {
      if (!controls.contains(document.activeElement)) controls.classList.add('quiet');
    }, 5000);
  };
  document.addEventListener('keydown', key);
  document.addEventListener('visibilitychange', visible);
  document.addEventListener('pointerdown', reveal);
  document.addEventListener('keydown', reveal);
  controls.addEventListener('focusin', reveal);
  viewport.addEventListener('pointerdown', down);
  viewport.addEventListener('pointerup', up);
  viewport.addEventListener('pointercancel', cancel);
  reveal();
  return () => {
    disposed = true;
    generation += 1;
    keepAwake = false;
    releaseLock();
    clearTimeout(quietTimer);
    document.removeEventListener('keydown', key);
    document.removeEventListener('visibilitychange', visible);
    document.removeEventListener('pointerdown', reveal);
    document.removeEventListener('keydown', reveal);
    controls.removeEventListener('focusin', reveal);
    viewport.removeEventListener('pointerdown', down);
    viewport.removeEventListener('pointerup', up);
    viewport.removeEventListener('pointercancel', cancel);
  };
}
