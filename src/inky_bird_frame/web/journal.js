// The controller owns birds and collections. No private journal enters browser storage.
let run = 0;
let stopPresentation = () => {};
let frameSignOut = null;
export function retainSignedOutFrame() {
  if (!frameSignOut) return false;
  frameSignOut(); return true;
}
export function closeJournal() {
  run += 1;
  stopPresentation(); stopPresentation = () => {};
  frameSignOut = null;
  document.querySelector('#journal-navigation')?.remove();
  document.body.classList.remove('journal', 'presenting');
}

const sourceNames = {
  inaturalist: ['iNaturalist', 'Community reports'], ebird: ['eBird nearby', 'Community reports'],
  'ebird-archive': ['eBird Archive', 'Your eBird checklists'], birdweather: ['BirdWeather', 'Station detections'],
  'birdnet-go': ['BirdNET-Go', 'Station detections'], 'birdnet-analyzer': ['BirdNET Analyzer', 'Imported detections'],
  birdbuddy: ['Bird Buddy', 'Feeder records']
};

export async function openJournal(ui, message = '') {
  closeJournal(); const pending = run;
  const data = await ui.api('/v1/owner/journal');
  if (run !== pending) return;
  const {element: el, button, api, screen} = ui;
  const main = document.querySelector('#main');
  const query = new URL(location.href).searchParams;
  const area = query.get('page') || 'home';
  const view = query.get('view') || 'current';
  const byId = new Map(data.birds.map(bird => [bird.taxon_id, bird]));
  function href(page, extra = {}) {
    return '/app?' + new URLSearchParams({page, ...extra});
  }
  function link(text, page, extra = {}, style = '') {
    const node = el('a', text, style); node.href = href(page, extra); return node;
  }
  function note(text) { return el('p', text, 'hint'); }
  function art(bird, className = '') {
    const image = el('img', '', className); image.src = bird.portrait_url;
    image.alt = 'Approved field-journal plate: ' + bird.common_name;
    image.loading = 'lazy'; image.decoding = 'async'; return image;
  }
  function evidence(bird) {
    return [...new Set(bird.sources.map(name => sourceNames[name]?.[1]).filter(Boolean))].join(' · ')
      || (bird.saved ? 'Saved selection' : bird.known ? 'Collection selection' : 'Available artwork · not a sighting');
  }
  function card(bird) {
    const node = link('', 'bird', {bird: bird.taxon_id}, 'bird-card');
    if (bird.plate_ready) node.append(art(bird));
    else node.append(el('div', 'Plate not ready', 'missing-art'));
    node.append(el('h2', bird.common_name), el('p', bird.scientific_name, 'scientific'), note(evidence(bird)));
    return node;
  }
  function members(key) {
    if (key === 'all') return data.birds;
    const ids = key === 'saved' ? data.saved_taxon_ids : key === 'current' ? data.active_taxon_ids
      : data.collections.find(group => group.collection_id === key)?.taxon_ids;
    if (!ids) throw new Error('This collection is no longer available. Open Collections and choose another.');
    return ids.map(id => byId.get(id)).filter(Boolean);
  }
  function viewLabel(key) {
    return key === 'all' ? 'All available artwork' : key === 'saved' ? 'Saved birds'
      : key === 'current' ? 'Current and saved' : data.collections.find(group => group.collection_id === key)?.label || 'Collection';
  }
  function base(title, description = '') {
    screen(title, description); document.body.classList.add('journal');
    const nav = el('nav', '', 'journal-navigation'); nav.id = 'journal-navigation';
    nav.setAttribute('aria-label', 'Journal');
    for (const [key, name] of [['home', 'Home'], ['birds', 'Birds'], ['collections', 'Collections'], ['manage', 'Manage']]) {
      const item = link(name, key); if (key === area) item.setAttribute('aria-current', 'page'); nav.append(item);
    }
    document.body.append(nav);
    if (message) main.append(el('p', message, 'connection'));
  }
  function empty(title, text) {
    const section = el('section', '', 'journal-card'); section.append(el('h2', title), el('p', text)); return section;
  }
  function playLink(key) { return link('Play', 'frame', {view: key}, 'primary-link'); }
  const management = data.management;
  if (area === 'home') {
    base('Your field journal', 'Birds worth keeping close.');
    const featured = byId.get(data.featured_taxon_id);
    if (featured) {
      const hero = link('', 'bird', {bird: featured.taxon_id}, 'featured-art'); hero.append(art(featured));
      main.append(hero, el('h2', featured.common_name), note(evidence(featured)), playLink('current'));
    } else main.append(empty('Make room for a bird', 'No approved plate is in your current selection yet. Browse available artwork or open a saved collection.'),
      link('Browse available artwork', 'birds', {scope: 'art'}, 'primary-link'));
    if (management.available) {
      const work = management.work;
      const attention = work.needs_attention.length + management.notifications.dead_letters;
      main.append(link(attention ? attention + ' items need attention' : 'Check sources and plate work', 'manage', {}, 'attention-link'));
      if (!work.complete) main.append(note('Discovery is ' + work.discovery_status + '. The last saved selection may be out of date.'));
      if (management.last_confirmed_panel_update) main.append(note('Last confirmed panel update: ' + new Date(management.last_confirmed_panel_update).toLocaleString()));
    }
  } else if (area === 'birds') {
    const artScope = query.get('scope') === 'art';
    base(artScope ? 'Available artwork' : 'Your birds', artScope
      ? 'Approved local plates. Browsing does not add a sighting or change your wall.'
      : 'Birds in your current selection or saved collections. A missing plate does not erase a bird.');
    main.append(link(artScope ? 'Show your birds' : 'Browse available artwork', 'birds', artScope ? {} : {scope: 'art'}));
    const label = el('label', 'Find a bird'); label.htmlFor = 'bird-search';
    const search = el('input'); search.type = 'search'; search.id = label.htmlFor; search.autocomplete = 'off';
    const count = el('p', '', 'hint'); count.setAttribute('role', 'status');
    const grid = el('div', '', 'bird-grid'); main.append(label, search, count, grid);
    function filter() {
      const term = search.value.trim().toLocaleLowerCase();
      const birds = data.birds.filter(bird => (artScope ? bird.plate_ready : bird.known)
        && (bird.common_name + ' ' + bird.scientific_name).toLocaleLowerCase().includes(term));
      grid.replaceChildren(...birds.map(card)); count.textContent = birds.length + (birds.length === 1 ? ' species' : ' species');
      if (!birds.length) grid.append(empty('No birds here yet', term ? 'Try another name.' : 'Connect a source or choose artwork for a collection.'));
    }
    search.addEventListener('input', filter); filter();
  } else if (area === 'bird') {
    const bird = byId.get(Number(query.get('bird')));
    if (!bird) { base('Bird unavailable'); main.append(link('Back to birds', 'birds')); return; }
    base(bird.common_name, bird.scientific_name);
    main.append(note(evidence(bird)));
    if (bird.plate_ready) {
      const image = art(bird, 'detail-art'); main.append(image);
      const result = await api('/v1/owner/journal/birds/' + bird.taxon_id + '/profile');
      if (!image.isConnected) return;
      if (result.profile) {
        const profile = result.profile;
        main.append(el('h2', 'Field notes'), note(profile.family));
        const facts = el('dl', '', 'measurements');
        for (const [key, value] of Object.entries(profile.measurements)) facts.append(el('dt', key[0].toUpperCase() + key.slice(1)), el('dd', value));
        const marks = el('ul'); for (const text of profile.field_marks) marks.append(el('li', text));
        main.append(facts, el('h2', 'Look for'), marks, el('h2', 'Habitat'), el('p', profile.habitat), el('h2', 'Behavior'), el('p', profile.behavior), el('h2', 'Sources'));
        const sources = el('ul');
        for (const source of profile.sources) {
          const item = el('li'), anchor = el('a', source.title); anchor.href = source.url;
          anchor.target = '_blank'; anchor.rel = 'noopener noreferrer'; item.append(anchor); sources.append(item);
        }
        main.append(sources);
      } else main.append(note(result.notice));
    } else main.append(empty('Plate not ready', 'This bird stays in your journal. Open Plate work to inspect its current state.'));
    const groups = data.collections.filter(group => group.taxon_ids.includes(bird.taxon_id));
    main.append(el('h2', 'In your collections'));
    for (const group of groups) main.append(link(group.label, 'collection', {view: group.collection_id}));
    main.append(link('Make a collection with this bird', 'edit-collection', {bird: bird.taxon_id}, 'primary-link'), link('Back to birds', 'birds'));
  } else if (area === 'collections') {
    base('Collections', 'Keep a selection together. Saving a group does not change your wall or request new art.');
    const grid = el('div', '', 'bird-grid');
    for (const key of ['current', 'saved', ...data.collections.map(group => group.collection_id), 'all']) {
      const birds = members(key); const ready = birds.filter(bird => bird.plate_ready);
      const node = el('section', '', 'journal-card');
      if (ready[0]) { const cover = link('', 'collection', {view: key}); cover.append(art(ready[0])); node.append(cover); }
      node.append(link(viewLabel(key), 'collection', {view: key}, 'collection-title'),
        note(ready.length + ' plates ready · ' + (birds.length - ready.length) + ' not ready'), playLink(key));
      grid.append(node);
    }
    main.append(link('New collection', 'edit-collection', {}, 'primary-link'), grid,
      note('Current and saved keeps the existing controller selection. Named collections do not join it automatically. Available artwork is not personal evidence.'));
  } else if (area === 'edit-collection') {
    const group = data.collections.find(item => item.collection_id === query.get('view'));
    const selected = new Set(group?.taxon_ids || [Number(query.get('bird'))].filter(Boolean));
    base(group ? 'Edit collection' : 'New collection', 'Choose species for a private saved selection. This does not claim that you saw them.');
    const form = el('form'); const label = el('label', 'Collection name'); label.htmlFor = 'collection-name';
    const name = el('input'); name.id = label.htmlFor; name.required = true; name.maxLength = 128; name.value = group?.label || '';
    const fields = el('fieldset'); fields.append(el('legend', 'Choose birds'));
    for (const bird of data.birds) {
      const row = el('label', '', 'check-label'); const input = el('input'); input.type = 'checkbox';
      input.checked = selected.has(bird.taxon_id);
      input.addEventListener('change', () => input.checked ? selected.add(bird.taxon_id) : selected.delete(bird.taxon_id));
      row.append(input, document.createTextNode(bird.common_name + (bird.plate_ready ? '' : ' · plate not ready'))); fields.append(row);
    }
    const problem = el('p', '', 'error'); problem.setAttribute('role', 'alert');
    const save = el('button', 'Save collection'); save.type = 'submit';
    form.append(label, name, fields, save, problem);
    form.addEventListener('submit', async event => {
      event.preventDefault(); save.disabled = true;
      try {
        const body = {revision: data.revision, label: name.value, taxon_ids: [...selected]};
        if (group) body.collection_id = group.collection_id;
        await api('/v1/owner/journal/collections/save', body);
        if (form.isConnected) location.assign(href('collections'));
      } catch (error) { if (error.code === 'sign_in_required') ui.showProblem(error); else problem.textContent = error.message; }
      finally { save.disabled = false; }
    });
    main.append(form, link('Cancel', 'collections'));
  } else if (area === 'collection') {
    const birds = members(view); base(viewLabel(view), view === 'all' ? 'Artwork, not a record of personal sightings.' : 'A saved view. Play does not change the physical wall.');
    main.append(playLink(view));
    const group = data.collections.find(item => item.collection_id === view);
    if (group) main.append(link('Edit collection', 'edit-collection', {view}), button('Remove collection', async () => {
      if (!confirm('Remove this collection? Source history, Saved birds and approved plates stay unchanged.')) return;
      try { await api('/v1/owner/journal/collections/remove', {revision: data.revision, collection_id: view}); location.assign(href('collections')); }
      catch (error) { ui.showProblem(error); }
    }));
    const grid = el('div', '', 'bird-grid'); grid.append(...birds.map(card));
    if (!birds.length) grid.append(empty('An empty collection is fine', 'Add birds when you are ready. Nothing outside this view will play.'));
    main.append(grid);
  } else if (area === 'frame') {
    let birds = members(view).filter(bird => bird.plate_ready); base(viewLabel(view));
    if (!birds.length) { main.append(empty('No approved plates in this view', 'Inky will not switch to another collection.'), link('Exit Frame', 'collections')); return; }
    document.body.classList.add('presenting'); document.querySelector('#journal-navigation')?.remove();
    let position = 0, paused = true, timer = null, expired = false;
    let image = el('img', '', 'frame-art'), objectURL = null, renderedKey = '', loading = null;
    image.hidden = true;
    const caption = el('p'), status = el('p', '', 'hint'); status.setAttribute('role', 'status');
    const controls = el('div', '', 'frame-controls');
    function signedOut() {
      if (expired) return;
      ui.sessionExpired();
      expired = true; pause(); closeJournal();
      image.alt = 'Last visible approved bird plate'; document.title = 'Inky';
      status.textContent = 'Signed out. The last approved image stays visible. Sign in to continue.';
      main.replaceChildren(image, status, link('Sign in', 'home'));
      byId.clear(); for (const key of Object.keys(data)) delete data[key]; birds = [];
    }
    async function show(index = position) {
      const bird = birds[index];
      if (!bird || expired) return;
      const key = bird.taxon_id + ':' + bird.portrait_sha256;
      loading?.abort();
      if (key === renderedKey) { position = index; status.textContent = ''; return; }
      const request = new AbortController(); loading = request;
      let nextURL;
      try {
        const response = await fetch(bird.portrait_url, {credentials: 'same-origin', cache: 'no-store', signal: request.signal});
        if (request.signal.aborted || !image.isConnected) return;
        if ([401, 403].includes(response.status)) { signedOut(); return; }
        if (!response.ok || response.headers.get('content-type')?.split(';')[0] !== 'image/png') throw new Error('Plate unavailable');
        nextURL = URL.createObjectURL(await response.blob());
        const nextImage = el('img', '', 'frame-art'); nextImage.src = nextURL;
        nextImage.alt = 'Approved plate: ' + bird.common_name;
        await nextImage.decode();
        if (request.signal.aborted || expired || !image.isConnected) return;
        image.replaceWith(nextImage); image = nextImage;
        if (objectURL) URL.revokeObjectURL(objectURL);
        objectURL = nextURL; nextURL = null; renderedKey = key; position = index;
        caption.textContent = bird.common_name; details.href = href('bird', {bird: bird.taxon_id});
        status.textContent = '';
      } catch (error) {
        if (!request.signal.aborted && !expired && image.isConnected) {
          pause(); status.textContent = 'Plate unavailable. Keeping the last approved plate. Try Next when the connection returns.';
        }
      } finally { if (nextURL) URL.revokeObjectURL(nextURL); }
    }
    function step(change) { show((position + change + birds.length) % birds.length); }
    function pause() { paused = true; clearInterval(timer); play.textContent = 'Play'; }
    const play = button('Play', () => {
      if (!paused) { pause(); return; }
      paused = false; play.textContent = 'Pause'; timer = setInterval(() => { if (!document.hidden && !expired) step(1); }, 60000);
    });
    const details = link('Details', 'bird', {bird: birds[0].taxon_id}); details.addEventListener('click', pause);
    controls.append(button('Previous', () => step(-1)), play, button('Next', () => step(1)), details, link('Exit Frame', 'collections'));
    if (document.documentElement.requestFullscreen) controls.append(button('Fullscreen', async () => {
      try { if (!document.fullscreenElement) await document.documentElement.requestFullscreen(); else await document.exitFullscreen(); }
      catch { status.textContent = 'This browser does not allow fullscreen here. Frame still works.'; }
    }));
    const key = event => { if (['INPUT', 'BUTTON', 'A'].includes(event.target.tagName)) return;
      if (event.key === 'ArrowRight') step(1); if (event.key === 'ArrowLeft') step(-1);
      if (event.key === ' ') { event.preventDefault(); play.click(); } };
    document.addEventListener('keydown', key);
    // Check authority, not activity. A display view must not renew an owner session.
    const check = setInterval(async () => {
      if (document.hidden) return;
      try {
        const current = await api('/v1/owner/journal');
        if (!image.isConnected || expired) return;
        const ids = view === 'all' ? current.birds.map(bird => bird.taxon_id) : view === 'current'
          ? current.active_taxon_ids : view === 'saved' ? current.saved_taxon_ids
          : current.collections.find(group => group.collection_id === view)?.taxon_ids;
        if (!ids || !ids.some(id => current.birds.some(bird => bird.taxon_id === id && bird.plate_ready))) {
          pause(); loading?.abort(); image.removeAttribute('src'); image.hidden = true; caption.textContent = '';
          controls.replaceChildren(link('Choose a collection', 'collections'));
          status.textContent = 'This view has no approved plates or was removed. Inky will not switch views.';
          clearInterval(check); return;
        }
        const previous = birds[position]?.taxon_id;
        birds = current.birds.filter(bird => ids.includes(bird.taxon_id) && bird.plate_ready);
        await show(Math.max(0, birds.findIndex(bird => bird.taxon_id === previous)));
      }
      catch (error) {
        if (['sign_in_required', 'secure_access_required'].includes(error.code)) {
          signedOut();
        } else { pause(); loading?.abort(); status.textContent = 'Offline. Keeping the last approved plate.'; }
      }
    }, 60000);
    stopPresentation = () => { clearInterval(timer); clearInterval(check); loading?.abort(); if (objectURL) URL.revokeObjectURL(objectURL); document.removeEventListener('keydown', key); };
    frameSignOut = signedOut;
    main.append(image, caption, controls, status, note('Plates change once a minute while playing. This does not refresh the wall panel.')); show();
  } else if (area === 'manage') {
    base('Manage', 'One controller. The same sources, reviewed plates and work rules.');
    const sections = [['sources', 'Sources', 'Check all seven supported inputs and the shared observation window.'],
      ['work', 'Plate work', 'Keep stopped, waiting and runnable work distinct.'],
      ['displays', 'Displays', 'Check the last confirmed physical panel update.'],
      ['notifications', 'Notifications', 'Check delivery separately from bird discovery.']];
    for (const [key, title, text] of sections) {
      const node = link('', key, {}, 'journal-card'); node.append(el('h2', title), el('p', text)); main.append(node);
    }
    main.append(button('Account and access', ui.account), button('Sign out', ui.signOut));
  } else if (['sources', 'work', 'displays', 'notifications'].includes(area)) {
    base({sources: 'Sources', work: 'Plate work', displays: 'Displays', notifications: 'Notifications'}[area]);
    main.append(link('Back to Manage', 'manage'));
    if (!management.available) { main.append(empty('Full configuration unavailable', 'Start Inky through its documented serve command to include controller settings.')); return; }
    if (area === 'sources') {
      main.append(note('Shared live window: ' + management.window + '. Scheduled refresh: every ' + management.refresh_minutes + ' minutes.'));
      for (const source of management.sources) {
        const [name, kind] = sourceNames[source.name]; const node = el('section', '', 'journal-card');
        const imported = ['ebird-archive', 'birdnet-analyzer'].includes(source.name);
        const state = !source.enabled ? 'Off' : source.status === 'ok'
          ? imported ? 'History read in last saved refresh' : source.species_count ? 'Last saved check succeeded' : 'Last saved check found no birds in this window'
          : source.status === 'error' ? 'Last saved check failed' : 'Not checked yet';
        node.append(el('h2', name), el('p', state, source.status === 'error' ? 'error' : 'connection'), note(kind));
        if (imported) node.append(note('Imported history is not a live connection.'));
        if (source.enabled && !management.work.complete) node.append(note('Discovery is ' + management.work.discovery_status + '. This is saved state, not a current connection test.'));
        if (source.unresolved_count) node.append(note(source.unresolved_count + ' unresolved species do not trigger generation.'));
        node.append(note('This installation uses managed configuration. The configuration file remains authoritative.'));
        main.append(node);
      }
    } else if (area === 'work') {
      if (!management.work.complete) main.append(el('p', 'Discovery is ' + management.work.discovery_status + '. Work counts may be incomplete.', 'notice'));
      for (const [key, label] of [['can_run', 'Can run'], ['waiting', 'Waiting until retry'], ['needs_attention', 'Needs attention']]) {
        const ids = management.work[key], section = el('section', '', 'journal-card'); section.append(el('h2', label + ' · ' + ids.length));
        for (const id of ids) section.append(link(byId.get(id)?.common_name || 'Taxon ' + id, 'bird', {bird: id})); main.append(section);
      }
      main.append(note('Past failed artifacts are not counted as runnable backlog. Browsing never starts a paid generation.'));
    } else if (area === 'displays') {
      main.append(playLink('current'), el('h2', 'Physical wall panel'), el('p', management.last_confirmed_panel_update
        ? 'Last confirmed panel update: ' + new Date(management.last_confirmed_panel_update).toLocaleString() : 'No confirmed panel update has been received.'),
      note('A browser view is not proof of a physical panel update. Existing wall selection and scheduling stay unchanged.'));
    } else {
      const state = management.notifications;
      main.append(el('p', state.enabled ? 'Notifications enabled' : 'Notifications off'), el('p', state.pending + ' pending · ' + state.dead_letters + ' dead letters'));
      for (const destination of state.destinations) main.append(el('p', destination.name + ' · ' + destination.events.join(', ')));
      main.append(note('Destination secrets stay on the controller. This managed configuration is changed through the existing operator tools.'));
    }
  } else { base('Page not found', 'Choose a place in your journal.'); }
}
