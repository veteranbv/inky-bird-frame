// The read-only screen has no owner cookie handling, private history or storage.
import { frameTools } from '/app/frame-tools.js';
const main = document.querySelector('#main');
const el = (tag, text = '') => {
  const node = document.createElement(tag);
  node.textContent = text;
  return node;
};
const button = (text, action) => {
  const node = el('button', text);
  node.type = 'button';
  node.addEventListener('click', action);
  return node;
};
let stopped = false,
  timer = null,
  objectURL = null,
  loading = null,
  sequence = 0,
  stopTools = () => {};
async function api(path, body) {
  let response;
  try {
    response = await fetch(path, {
      method: body === undefined ? 'GET' : 'POST',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw Object.assign(new Error('Offline. Keep this page open and check your connection.'), {
      code: 'offline',
    });
  }
  let result;
  try {
    result = await response.json();
  } catch {
    throw new Error('Inky is not responding. Try again shortly.');
  }
  if (!response.ok || !result.ok)
    throw Object.assign(new Error(result.error?.message || 'Screen access is unavailable.'), {
      code: result.error?.code,
    });
  return result.data;
}
function clearArt() {
  stopTools();
  stopTools = () => {};
  sequence += 1;
  loading?.abort();
  if (objectURL) URL.revokeObjectURL(objectURL);
  objectURL = null;
  main.querySelector('img')?.remove();
  document.body.classList.remove('presenting');
  document.title = 'Inky screen';
}
async function forget() {
  stopped = true;
  clearTimeout(timer);
  clearArt();
  await api('/v1/screen/forget', {});
  location.reload();
}
function unavailable(message) {
  stopped = true;
  clearTimeout(timer);
  clearArt();
  main.replaceChildren(
    el('h1', 'Screen access ended'),
    el('p', message),
    button('Pair this screen again', pair),
  );
}
async function pair() {
  stopped = false;
  clearArt();
  clearTimeout(timer);
  try {
    const pending = await api('/v1/screen/pair', {});
    if (pending.status === 'viewer') {
      await start();
      return;
    }
    const code = el('p', pending.pairing_code);
    code.className = 'pairing-code';
    const status = el('p', 'Waiting for the owner. This request lasts ten minutes.');
    status.setAttribute('role', 'status');
    main.replaceChildren(
      el('h1', 'Pair this screen'),
      code,
      el(
        'p',
        'On your signed-in device, open Manage → Displays. Match this code, name this screen and choose the view it may show.',
      ),
      el(
        'p',
        'Pairing permits approved artwork only. It does not sign this screen in as the owner.',
      ),
      status,
      button('Cancel pairing', forget),
    );
    let attempts = 0;
    const check = async () => {
      if (stopped || ++attempts > 40) {
        unavailable('Pairing expired. Start a new request.');
        return;
      }
      try {
        const view = await api('/v1/screen/view');
        if (!stopped) present(view);
      } catch (error) {
        if (stopped) return;
        status.textContent =
          error.code === 'offline' ? error.message : 'Waiting for owner approval.';
        timer = setTimeout(check, 15000);
      }
    };
    timer = setTimeout(check, 15000);
  } catch (error) {
    unavailable(error.message);
  }
}
function present(initial) {
  clearTimeout(timer);
  stopped = false;
  document.body.classList.add('presenting');
  let birds = initial.species,
    position = 0,
    paused = false,
    shuffle = false,
    rendered = '';
  const image = el('img'),
    caption = el('p'),
    status = el('p');
  image.className = 'frame-art';
  image.hidden = true;
  status.setAttribute('role', 'status');
  const controls = el('div');
  controls.className = 'frame-controls';
  const play = button('Pause', () => {
    paused = !paused;
    play.textContent = paused ? 'Play' : 'Pause';
  });
  const previous = button('Previous', () => show((position - 1 + birds.length) % birds.length));
  const next = button('Next', () => show((position + 1) % birds.length));
  const random = button('Shuffle off', () => {
    shuffle = !shuffle;
    random.textContent = shuffle ? 'Shuffle on' : 'Shuffle off';
  });
  controls.append(previous, play, next, random, button('Forget this screen', forget));
  const viewport = el('div');
  viewport.className = 'frame-viewport';
  viewport.append(image);
  stopTools = frameTools({
    viewport,
    controls,
    status,
    button,
    previous: () => previous.click(),
    next: () => next.click(),
    togglePlay: () => play.click(),
  });
  main.replaceChildren(viewport, caption, controls, status);
  function withdraw() {
    sequence += 1;
    loading?.abort();
    if (objectURL) URL.revokeObjectURL(objectURL);
    objectURL = null;
    rendered = '';
    image.removeAttribute('src');
    image.hidden = true;
    caption.textContent = '';
  }
  async function show(index = position) {
    const bird = birds[index];
    if (!bird) {
      withdraw();
      status.textContent = 'No approved plates in the permitted view. Inky will not switch views.';
      return;
    }
    position = index;
    const key = bird.taxon_id + ':' + bird.portrait_sha256;
    if (rendered === key) {
      status.textContent = '';
      return;
    }
    loading?.abort();
    const controller = new AbortController();
    loading = controller;
    const pending = ++sequence;
    try {
      const response = await fetch(bird.portrait_url, {
        credentials: 'same-origin',
        cache: 'no-store',
        signal: controller.signal,
      });
      if ([401, 403].includes(response.status)) {
        unavailable('Access expired or was revoked.');
        return;
      }
      if (!response.ok) throw new Error('The selected plate is temporarily unavailable.');
      const url = URL.createObjectURL(await response.blob());
      const decoded = new Image();
      decoded.src = url;
      try {
        await decoded.decode();
      } catch {
        URL.revokeObjectURL(url);
        throw new Error('This image could not be displayed.');
      }
      if (stopped || pending !== sequence) {
        URL.revokeObjectURL(url);
        return;
      }
      if (objectURL) URL.revokeObjectURL(objectURL);
      objectURL = url;
      image.src = url;
      image.hidden = false;
      image.alt = 'Approved field-journal plate: ' + bird.common_name;
      caption.textContent = bird.common_name;
      rendered = key;
      status.textContent = '';
    } catch (error) {
      if (!controller.signal.aborted && !stopped)
        status.textContent = error.message + ' Keeping the last approved image.';
    }
  }
  let turn = Date.now();
  async function poll() {
    if (stopped) return;
    if (!document.hidden) {
      try {
        const view = await api('/v1/screen/view');
        if (stopped) return;
        const id = birds[position]?.taxon_id;
        birds = view.species;
        if (
          rendered &&
          !birds.some((bird) => bird.taxon_id + ':' + bird.portrait_sha256 === rendered)
        )
          withdraw();
        position = Math.max(
          0,
          birds.findIndex((bird) => bird.taxon_id === id),
        );
        if (!paused && Date.now() - turn >= 60000) {
          position = shuffle
            ? Math.floor(Math.random() * birds.length)
            : (position + 1) % birds.length;
          turn = Date.now();
        }
        await show(position);
      } catch (error) {
        if (error.code === 'sign_in_required' || error.code === 'secure_access_required') {
          unavailable(error.message);
          return;
        }
        status.textContent = error.message + ' Keeping the last approved image.';
      }
    }
    timer = setTimeout(poll, 15000);
  }
  show();
  timer = setTimeout(poll, 15000);
}
async function start() {
  try {
    present(await api('/v1/screen/view'));
  } catch (error) {
    main.replaceChildren(
      el('h1', 'Show birds here'),
      el(
        'p',
        error.code === 'offline'
          ? error.message
          : 'Pair this browser to show one approved view without owner access.',
      ),
      button('Pair this screen', pair),
    );
  }
}
addEventListener('pagehide', () => {
  stopped = true;
  clearTimeout(timer);
  loading?.abort();
  stopTools();
  if (objectURL) URL.revokeObjectURL(objectURL);
});
await start();
