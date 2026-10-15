// The controller owns birds and collections. No private journal enters browser storage.
import {renderSettings} from '/app/settings.js';
import {renderImports} from '/app/imports.js';
import {renderPresentation} from '/app/presentation.js';
import {frameTools} from '/app/frame-tools.js';
let run = 0;
let stopScreen = () => {};
let frameSignOut = null;
export function retainSignedOutFrame() {
  if (!frameSignOut) return false;
  frameSignOut(); return true;
}
export function closeJournal() {
  run += 1;
  stopScreen(); stopScreen = () => {};
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
  function link(text, page, extra = {}, style = 'text-link') {
    const node = el('a', text, style); node.href = href(page, extra); return node;
  }
  function note(text) { return el('p', text, 'hint'); }
  function art(bird, className = '') {
    const image = el('img', '', className); image.src = bird.portrait_url;
    image.alt = 'Approved field-journal plate: ' + bird.common_name;
    image.loading = 'lazy'; image.decoding = 'async'; return image;
  }
  function evidence(bird) {
    return [...new Set([...bird.sources, ...(bird.retained_evidence || []).map(item => item.source)].map(name => sourceNames[name]?.[1]).filter(Boolean))].join(' · ')
      || (bird.sources.includes('owner-selection') ? 'Plate request or review · not a sighting'
        : bird.saved ? 'Saved selection' : bird.known ? 'Collection selection' : 'Available artwork · not a sighting');
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
    const ids = key === 'favorites' ? data.collections.find(group => group.kind === 'favorites')?.taxon_ids || []
      : key === 'saved' ? data.saved_taxon_ids : key === 'current' ? data.active_taxon_ids
      : data.collections.find(group => group.collection_id === key)?.taxon_ids;
    if (!ids) throw new Error('This collection is no longer available. Open Collections and choose another.');
    return ids.map(id => byId.get(id)).filter(Boolean);
  }
  function viewLabel(key) {
    return key === 'favorites' ? 'Favorites' : key === 'all' ? 'All available artwork' : key === 'saved' ? 'Saved birds'
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
  const operationNames = {refresh: 'Refresh observations', retry: 'Make a plate eligible for retry',
    'notification-test': 'Test notification delivery', 'notification-retry': 'Retry notification dead letters',
    'import-preview': 'Check history file', 'import-commit': 'Confirm history update', 'trip-preview': 'Review trip selection', 'trip-save': 'Save trip', 'queue-plates': 'Queue missing plates', 'approve-candidate': 'Approve candidate locally', 'reject-candidate': 'Reject candidate', 'community-preview': 'Check community history', 'community-save': 'Save community collection'};
  function operationAction(label, kind, explanation, taxonId) {
    const section = el('section', '', 'journal-card');
    let preview = null;
    const start = button(label, async () => {
      if (preview) { preview.querySelector('button')?.focus(); return; }
      preview = el('div', '', 'notice');
      const problem = el('p', '', 'error'); problem.setAttribute('role', 'alert');
      let revision, key;
      try {
        const settings = await api('/v1/owner/settings'); revision = settings.revision;
        if (!revision) throw new Error('This service has no authoritative configuration file.');
        key = Array.from(crypto.getRandomValues(new Uint8Array(24)), item => item.toString(16).padStart(2, '0')).join('');
      } catch (error) { preview = null; ui.showProblem(error); return; }
      preview.append(el('h2', operationNames[kind]), note(explanation),
        button('Confirm action', async event => {
          const control = event.currentTarget; control.disabled = true;
          try {
            const body = {kind, key, revision, confirm: true}; if (taxonId) body.taxon_id = taxonId;
            const receipt = await api('/v1/owner/operations', body);
            location.assign(href('operation', {operation: receipt.operation_id}));
          } catch (error) {
            if (error.code === 'sign_in_required') ui.showProblem(error);
            else { problem.textContent = error.message + ' Check recent operations if the connection was lost.'; control.disabled = false; }
          }
        }), button('Cancel', () => { preview.remove(); preview = null; start.focus(); }), problem);
      section.append(preview); preview.querySelector('button').focus();
    }); section.append(start); return section;
  }
  function savedAction(label, action, taxonId) {
    const section=el('section','','journal-card'), problem=el('p','','error');problem.setAttribute('role','alert');
    const start=button(label,async()=>{
      start.disabled=true;
      try{
        const settings=await api('/v1/owner/settings'), body={action,configuration_revision:settings.revision};if(taxonId)body.taxon_id=taxonId;
        const preview=await api('/v1/owner/saved/preview',body), key=Array.from(crypto.getRandomValues(new Uint8Array(24)),value=>value.toString(16).padStart(2,'0')).join('');
        section.append(el('h2','Change Saved birds?'),note(preview.added_count+' additions · '+preview.removed_count+' removals · '+preview.migrated_legacy_queue_count+' legacy seed members migrated'),note('Saved birds join the compatible current selection and can affect the existing wall immediately at its next refresh. This is curation, not a sighting. Other named collections and source history stay unchanged. No model starts.'),button('Confirm Saved birds change',async event=>{
          const control=event.currentTarget;control.disabled=true;
          try{const receipt=await api('/v1/owner/saved/change',{...body,key,confirm:true,collection_revision:preview.collection_revision});location.assign(href('operation',{operation:receipt.operation_id}));}
          catch(error){problem.textContent=error.message+' Check Saved birds if the connection was lost.';control.disabled=false;}
        }),button('Cancel',()=>location.reload()));
      }catch(error){problem.textContent=error.message;start.disabled=false;}
    });section.append(start,problem);return section;
  }
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
      const attention = work.needs_attention.length + work.in_review.candidates.length + management.notifications.dead_letters;
      main.append(link(attention ? attention + ' items need attention' : 'Check sources and plate work', 'manage', {}, 'attention-link'));
      if (!work.complete) main.append(note('Discovery is ' + work.discovery_status + '. The last saved selection may be out of date.'));
      if (management.last_confirmed_panel_update) main.append(note('Last confirmed panel update: ' + new Date(management.last_confirmed_panel_update).toLocaleString()));
    }
  } else if (area === 'birds') {
    const artScope = query.get('scope') === 'art';
    base(artScope ? 'Available artwork' : 'Your birds', artScope
      ? 'Approved local plates. Browsing does not add a sighting or change your wall.'
      : 'Birds in retained history, current observations or saved collections. A missing plate does not erase a bird.');
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
    if (!artScope) {
      const history = el('details'); history.append(el('summary', 'History coverage and unmatched species'));
      for (const source of data.history_coverage || []) history.append(note((sourceNames[source.source]?.[0] || source.source) + ': ' + source.coverage));
      for (const member of data.unresolved_history || []) history.append(note('Unmatched: ' + (member.scientific_name || member.common_name) + ' · ' + (sourceNames[member.source]?.[0] || member.source) + ' · ' + member.count + ' ' + member.unit));
      history.append(note('Unmatched names stay private. They cannot request artwork until an exact taxon match exists. Viewing history does not contact a source or change generation eligibility. Live sources without retained local history describe only their last discovery window.'));
      main.append(history);
    }
  } else if (area === 'bird') {
    const bird = byId.get(Number(query.get('bird')));
    if (!bird) { base('Bird unavailable'); main.append(link('Back to birds', 'birds')); return; }
    base(bird.common_name, bird.scientific_name);
    main.append(note(evidence(bird)));
    for (const item of bird.retained_evidence || []) main.append(note((sourceNames[item.source]?.[0] || item.source) + ': ' + item.count + ' ' + item.unit + ' in retained history. Counts from different sources are not added together.'));
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
    const favorite = groups.some(group => group.kind === 'favorites');
    main.append(button(favorite ? 'Remove from Favorites' : 'Add to Favorites', async event => {
      const control = event.currentTarget; control.disabled = true;
      try {
        await api('/v1/owner/journal/collections/favorite', {revision: data.revision, taxon_id: bird.taxon_id, favorite: !favorite});
        location.reload();
      } catch (error) { control.disabled = false; ui.showProblem(error); }
    }), note('Favorites are bookmarks. They do not record sightings, request art or change your wall.'));
    main.append(link('Make a collection with this bird', 'edit-collection', {bird: bird.taxon_id}, 'primary-link'), link('Back to birds', 'birds'));
    if(bird.saved || bird.plate_ready)main.append(savedAction(bird.saved ? 'Review removal from Saved birds' : 'Review adding to Saved birds',bird.saved?'remove':'add',bird.taxon_id));
  } else if (area === 'collections') {
    base('Collections', 'Keep a selection together. Saving a group does not change your wall or request new art.');
    const grid = el('div', '', 'bird-grid');
    for (const key of ['current', 'saved', 'favorites', ...data.collections.filter(group => group.kind !== 'favorites').map(group => group.collection_id), 'all']) {
      const birds = members(key); const ready = birds.filter(bird => bird.plate_ready);
      const node = el('section', '', 'journal-card');
      if (ready[0]) { const cover = link('', 'collection', {view: key}); cover.append(art(ready[0])); node.append(cover); }
      node.append(link(viewLabel(key), 'collection', {view: key}, 'collection-title'),
        note(ready.length + ' plates ready · ' + (birds.length - ready.length) + ' not ready'), playLink(key));
      grid.append(node);
    }
    main.append(link('New collection', 'edit-collection', {}, 'primary-link'), link('Make a trip from your eBird checklists', 'imports', {kind: 'ebird-archive'}), link('Birds reported near a place', 'community'), link('Import a collection', 'import-collection'), grid,
      note('Current and saved keeps the existing controller selection. Named collections do not join it automatically. Available artwork is not personal evidence.'));
  } else if (area === 'community') {
    base('Birds reported near a place', 'Search iNaturalist community reports. These are not your personal sightings. The query sends this point, radius and dates to iNaturalist.');
    const preview = await api('/v1/owner/community');
    const key = () => Array.from(crypto.getRandomValues(new Uint8Array(24)), item => item.toString(16).padStart(2, '0')).join('');
    if (preview.status === 'ready') {
      const group = preview.collection;
      main.append(el('h2', group.label), note(group.start_date + ' through ' + group.end_date + ' · inclusive dates'), note(group.members.length + ' species · complete search result'), note(preview.notice));
      const list = el('ul'); for (const member of group.members) list.append(el('li', member.common_name + ' · ' + member.scientific_name)); main.append(list);
      const requestKey = key();
      main.append(button('Save community collection', async () => {
        try { const receipt = await api('/v1/owner/community/save', {key: requestKey, preview_hash: preview.preview_hash, confirm: true}); location.assign(href('operation', {operation: receipt.operation_id})); }
        catch (error) { ui.showProblem(error); }
      }));
    }
    main.append(button(preview.status === 'ready' ? 'Discard preview and search again' : 'New community search', () => {
      const form = el('form'), inputs = {};
      for (const [name, title, type] of [['label','Collection name','text'],['start_date','Start date','date'],['end_date','End date','date'],['latitude','Latitude','number'],['longitude','Longitude','number'],['radius_km','Radius in km','number']]) {
        const label = el('label', title), input = el('input'); input.id = 'community-' + name; label.htmlFor = input.id; input.type = type; input.required = true;
        if (name === 'latitude' || name === 'longitude') { input.step = 'any'; input.min = name === 'latitude' ? '-90' : '-180'; input.max = name === 'latitude' ? '90' : '180'; }
        if (name === 'radius_km') { input.min = '1'; input.max = '500'; input.step = '1'; }
        if (name === 'label') input.maxLength = 128;
        inputs[name] = input; form.append(label, input);
      }
      form.append(note('Searches must contain no more than 500 species. Narrow a larger result; Inky never saves a truncated selection.'), note('A successful new search replaces only the temporary preview, not saved collections.'));
      const submit = el('button', 'Check community reports'); submit.type = 'submit'; const problem = el('p', '', 'error'); problem.setAttribute('role', 'alert'); form.append(submit, problem);
      let requestKey = key();
      form.addEventListener('input', () => { requestKey = key(); });
      form.addEventListener('submit', async event => {
        event.preventDefault(); submit.disabled = true;
        try { const receipt = await api('/v1/owner/community/preview', {key: requestKey, choices: Object.fromEntries(Object.entries(inputs).map(([name,input])=>[name,input.value])), confirm: true}); location.assign(href('operation', {operation: receipt.operation_id})); }
        catch (error) { problem.textContent = error.message; submit.disabled = false; }
      }); main.append(form); inputs.label.focus();
    }), link('Back to Collections', 'collections'));
  } else if (area === 'edit-collection') {
    const group = data.collections.find(item => item.collection_id === query.get('view'));
    if (group?.kind === 'favorites') throw new Error('Add or remove Favorites on each bird’s page.');
    const selected = new Set(group?.taxon_ids || [Number(query.get('bird'))].filter(Boolean));
    base(group ? 'Edit collection' : 'New collection', 'Choose species for a private saved selection. This does not claim that you saw them.');
    if (group?.kind === 'personal-trip') main.append(note('Existing checklist links stay with retained members. Any bird you add here is a manual selection, not a new personal sighting. Unresolved source names remain until a reviewed history selection replaces them.'));
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
  } else if (area === 'import-collection') {
    base('Import a collection', 'Bring a saved selection to this controller. Imported labels are not proof that you saw a bird.');
    const label = el('label', 'Inky collection file'); label.htmlFor = 'collection-file';
    const input = el('input'); input.id = label.htmlFor; input.type = 'file'; input.accept = '.json,application/json';
    const content = el('section', '', 'journal-card'); main.append(label, input, content, link('Cancel', 'collections'));
    let selection = 0;
    input.addEventListener('change', async () => {
      const pending = ++selection; content.replaceChildren();
      try {
        const file = input.files[0]; if (!file) return;
        if (file.size > 1024 * 1024) throw new Error('Choose a collection file no larger than 1 MiB.');
        const document = JSON.parse(await file.text());
        const preview = await api('/v1/owner/journal/collections/import-preview', {document});
        if (selection !== pending || !content.isConnected) return;
        content.append(el('h2', preview.label), note(preview.resolved + ' matched species · ' + preview.ready + ' approved plates · ' + preview.unresolved.length + ' unresolved'), note(preview.notice));
        if (preview.start_date) content.append(note(preview.start_date + ' through ' + preview.end_date + ' · inclusive dates'));
        for (const member of preview.unresolved) content.append(note('Unresolved: ' + (member.scientific_name || member.common_name)));
        const collection_id = Array.from(crypto.getRandomValues(new Uint8Array(16)), item => item.toString(16).padStart(2, '0')).join('');
        const problem = el('p', '', 'error'); problem.setAttribute('role', 'alert');
        content.append(button('Import this selection', async event => {
          const control = event.currentTarget; control.disabled = true;
          try {
            await api('/v1/owner/journal/collections/import', {document, revision: data.revision, preview_hash: preview.preview_hash, collection_id});
            location.assign(href('collection', {view: collection_id}));
          } catch (error) {
            control.disabled = false;
            if (error.code === 'sign_in_required') ui.showProblem(error);
            else problem.textContent = error.message + ' You can retry this same save safely, or check Collections for its result.';
          }
        }), problem, link('Check Collections', 'collections'));
      } catch (error) { if (selection === pending) content.replaceChildren(el('p', error instanceof SyntaxError ? 'This file is not valid JSON.' : error.message, 'error')); }
    });
  } else if (area === 'collection') {
    const birds = members(view); base(viewLabel(view), view === 'all' ? 'Artwork, not a record of personal sightings.' : 'A saved view. Play does not change the physical wall.');
    main.append(playLink(view));
    if(view==='saved')main.append(savedAction('Review adding all available approved art','import-approved'));
    const group = data.collections.find(item => item.collection_id === view);
    if (group) {
      main.append(note(group.kind === 'personal-trip' ? 'Personal trip · linked eBird checklists' : group.kind === 'community-history' ? 'Community reports · not your personal sightings' : group.kind === 'imported-selection' ? 'Imported selection · personal evidence not verified here' : group.kind === 'favorites' ? 'Bookmarks · not sightings' : 'Saved selection · not a sighting record'));
      if (group.start_date) main.append(note(group.start_date + ' through ' + group.end_date + ' · inclusive dates'));
      for (const member of group.unresolved) main.append(note('Unresolved: ' + (member.scientific_name || member.common_name) + ' · no generation requested'));
      for (const member of group.evidence || []) if (member.status !== 'linked') main.append(el('p', (byId.get(member.taxon_id)?.common_name || member.scientific_name) + ': ' + (member.status === 'source-changed' ? 'Source record changed. Your curated selection stays, but its original personal evidence no longer matches.' : 'Source history is unavailable. Inky cannot confirm this member’s evidence.'), 'notice'));
      main.append(button('Export collection', async () => {
        if (!confirm('This file includes this collection’s name, dates and species. It does not include credentials or private source identifiers. Check those details before sharing it.')) return;
        try {
          const portable = await api('/v1/owner/journal/collections/export?collection=' + encodeURIComponent(group.collection_id));
          const url = URL.createObjectURL(new Blob([JSON.stringify(portable, null, 2) + '\n'], {type: 'application/json'}));
          const anchor = el('a'); anchor.href = url; anchor.download = 'inky-collection.json'; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
        } catch (error) { ui.showProblem(error); }
      }));
      const request = el('section', '', 'journal-card'), problem = el('p', '', 'error'); problem.setAttribute('role', 'alert');
      request.append(button('Review missing-plate requests', async event => {
        const control = event.currentTarget; control.disabled = true;
        try {
          const settings = await api('/v1/owner/settings');
          const choices = {collection_id: group.collection_id, revision: data.revision, configuration_revision: settings.revision};
          const preview = await api('/v1/owner/journal/collections/queue-preview', choices), plan = preview.plan;
          const summary = el('div', '', 'notice');
          summary.append(el('h2', 'Queue missing plates?'), note(plan.new_requests.length + ' new requests · ' + plan.already_queued.length + ' already queued · ' + plan.held.length + ' held · ' + plan.ready.length + ' ready · ' + preview.unresolved + ' unresolved'));
          for (const id of plan.held) {
            const pending = management.work?.in_review.candidates.some(candidate => candidate.taxon_id === id && candidate.inspect_available);
            summary.append(note((byId.get(id)?.common_name || 'Taxon ' + id) + ': existing review or recovery hold. This action will not clear it.'), pending ? link('Inspect the pending candidate', 'candidate', {bird: id}) : link('Inspect this bird’s recovery', 'work', {bird: id}));
          }
          for (const item of plan.waiting) summary.append(note((byId.get(item.taxon_id)?.common_name || 'Taxon ' + item.taxon_id) + ': retry not before ' + new Date(item.next_attempt_at).toLocaleString()));
          summary.append(note(settings.generation_enabled ? 'Automatic generation is enabled. Eligible requests can use your AI allowance or incur costs at a scheduled cycle.' : 'Generation is off. Requests stay queued until you deliberately enable it.'), note('This does not start a model, add Saved birds, clear a hold, reset retry timing or change the wall. Unresolved identities cannot request a plate.'));
          if (plan.new_requests.length) {
            const key = Array.from(crypto.getRandomValues(new Uint8Array(16)), item => item.toString(16).padStart(2, '0')).join('');
            summary.append(button('Confirm these plate requests', async event => {
              const confirm = event.currentTarget; confirm.disabled = true;
              try { const receipt = await api('/v1/owner/journal/collections/queue', {...choices, preview_hash: preview.preview_hash, key, confirm: true}); location.assign(href('operation', {operation: receipt.operation_id})); }
              catch (error) { confirm.disabled = false; if (error.code === 'sign_in_required') ui.showProblem(error); else problem.textContent = error.message + ' Check recent operations if the response was lost.'; }
            }));
          }
          summary.append(button('Close preview', () => { summary.remove(); control.disabled = false; control.focus(); })); request.append(summary); summary.querySelector('button').focus();
        } catch (error) { control.disabled = false; if (error.code === 'sign_in_required') ui.showProblem(error); else problem.textContent = error.message; }
      }), problem); main.append(request);
    }
    if (group && group.kind !== 'favorites') main.append(link('Edit collection', 'edit-collection', {view}), button('Remove collection', async () => {
      if (!confirm('Remove this collection? Source history, Saved birds and approved plates stay unchanged.')) return;
      try { await api('/v1/owner/journal/collections/remove', {revision: data.revision, collection_id: view}); location.assign(href('collections')); }
      catch (error) { ui.showProblem(error); }
    }));
    const grid = el('div', '', 'bird-grid');
    for (const bird of birds) {
      const node = card(bird), origin = group?.member_origins.find(member => member.taxon_id === bird.taxon_id)?.origin;
      if (group?.kind === 'personal-trip') {
        const linked = group.evidence?.find(member => member.taxon_id === bird.taxon_id)?.status;
        node.append(note(origin === 'ebird-checklist' && linked === 'linked' ? 'Your eBird checklist · linked within this trip’s dates' : origin === 'ebird-checklist' ? 'Saved trip member · personal evidence needs attention' : 'Manual selection · not a personal sighting record'));
      } else if (group?.kind === 'imported-selection') node.append(note('Imported selection · personal evidence not verified here'));
      else if (group?.kind === 'community-history') node.append(note('Community report · not your personal sighting'));
      grid.append(node);
    }
    if (!birds.length) grid.append(empty('An empty collection is fine', 'Add birds when you are ready. Nothing outside this view will play.'));
    main.append(grid);
  } else if (area === 'frame') {
    let birds = members(view).filter(bird => bird.plate_ready); base(viewLabel(view));
    if (!birds.length) { main.append(empty('No approved plates in this view', 'Inky will not switch to another collection.'), link('Exit Frame', 'collections')); return; }
    document.body.classList.add('presenting'); document.querySelector('#journal-navigation')?.remove();
    let position = 0, paused = true, timer = null, expired = false, shuffle = false;
    let image = el('img', '', 'frame-art'), objectURL = null, renderedKey = '', loading = null;
    image.hidden = true;
    const caption = el('p'), status = el('p', '', 'hint'); status.setAttribute('role', 'status');
    const controls = el('div', '', 'frame-controls');
    const viewport = el('div', '', 'frame-viewport'); viewport.append(image);
    function signedOut() {
      if (expired) return;
      ui.sessionExpired();
      expired = true; pause(); closeJournal();
      image.alt = 'Last visible approved bird plate'; document.title = 'Inky';
      status.textContent = 'Signed out. The last approved image stays visible. Sign in, or pair this screen for continuous read-only viewing.';
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
    function step(change) { if (!document.hidden) show((position + change + birds.length) % birds.length); }
    function pause() { paused = true; clearInterval(timer); play.textContent = 'Play'; }
    const play = button('Play', () => {
      if (!paused) { pause(); return; }
      paused = false; play.textContent = 'Pause'; timer = setInterval(() => { if (!document.hidden && !expired) step(shuffle && birds.length>1 ? 1+Math.floor(Math.random()*(birds.length-1)) : 1); }, 60000);
    });
    const details = link('Details', 'bird', {bird: birds[0].taxon_id}); details.addEventListener('click', pause);
    controls.append(button('Previous', () => step(-1)), play, button('Next', () => step(1)), details, link('Exit Frame', 'collections'));
    const random=button('Shuffle off',()=>{shuffle=!shuffle;random.textContent=shuffle?'Shuffle on':'Shuffle off';});controls.append(random);
    const stopTools=frameTools({viewport,controls,status,button,previous:()=>step(-1),next:()=>step(1),togglePlay:()=>play.click()});
    // Check authority, not activity. A display view must not renew an owner session.
    const check = setInterval(async () => {
      if (document.hidden) return;
      try {
        const current = await api('/v1/owner/journal');
        if (!image.isConnected || expired) return;
        const ids = view === 'favorites' ? current.collections.find(group => group.kind === 'favorites')?.taxon_ids || []
          : view === 'all' ? current.birds.map(bird => bird.taxon_id) : view === 'current'
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
        if(renderedKey && !birds.some(bird=>bird.taxon_id+':'+bird.portrait_sha256===renderedKey)) {
          loading?.abort();if(objectURL)URL.revokeObjectURL(objectURL);objectURL=null;renderedKey='';image.removeAttribute('src');image.hidden=true;caption.textContent='';
        }
        await show(Math.max(0, birds.findIndex(bird => bird.taxon_id === previous)));
      }
      catch (error) {
        if (['sign_in_required', 'secure_access_required'].includes(error.code)) {
          signedOut();
        } else { pause(); loading?.abort(); status.textContent = 'Offline. Keeping the last approved plate.'; }
      }
    }, 60000);
    stopScreen = () => { clearInterval(timer); clearInterval(check); loading?.abort(); if (objectURL) URL.revokeObjectURL(objectURL); stopTools(); };
    frameSignOut = signedOut;
    main.append(viewport, caption, controls, status, note('Plates change once a minute while playing. This does not refresh the wall panel.')); show();
  } else if (area === 'candidate') {
    base('Review candidate', 'Generated art is not approved artwork. Inspect the complete plate before deciding.');
    main.append(link('Back to plate work', 'work'));
    const candidate = await api('/v1/owner/candidates/' + encodeURIComponent(query.get('bird') || ''));
    const content = el('section', '', 'journal-card'); content.append(el('h2', candidate.common_name), note(candidate.scientific_name), note(candidate.notice));
    const image = el('img', '', 'detail-art'); image.src = candidate.portrait_url; image.alt = 'Unapproved candidate: ' + candidate.common_name;
    content.append(image, el('h2', 'Saved quality review'));
    const scores = el('dl', '', 'measurements');
    for (const [name, score] of Object.entries(candidate.quality_review)) scores.append(el('dt', name.replaceAll('_', ' ')), el('dd', String(score)));
    content.append(scores);
    if (candidate.profile) {
      content.append(el('h2', 'Candidate field notes'), note(candidate.profile.family));
      for (const [name, value] of Object.entries(candidate.profile.measurements)) content.append(el('p', name + ': ' + value));
      const marks = el('ul'); for (const mark of candidate.profile.field_marks) marks.append(el('li', mark));
      content.append(marks, el('p', candidate.profile.habitat), el('p', candidate.profile.behavior));
    }
    content.append(el('h2', 'Review sources'));
    for (const source of candidate.verification_sources) { const link = el('a', source.title, 'text-link'); link.href = source.url; link.target = '_blank'; link.rel = 'noopener noreferrer'; content.append(link); }
    let loaded = false; image.addEventListener('load', () => { loaded = true; });
    if (image.complete && image.naturalWidth) loaded = true;
    const problem = el('p', '', 'error'); problem.setAttribute('role', 'alert');
    function decision(approve, reason = '') {
      const key = Array.from(crypto.getRandomValues(new Uint8Array(16)), item => item.toString(16).padStart(2, '0')).join('');
      let request = null;
      return button(approve ? 'Confirm local approval' : 'Confirm rejection', async event => {
        const control = event.currentTarget; control.disabled = true;
        try {
          if (approve && !loaded) throw new Error('The full candidate image must load before you can approve it.');
          if (!request) { const settings = await api('/v1/owner/settings'); request = {key, configuration_revision: settings.revision, candidate_revision: candidate.candidate_revision, confirm: true}; if (!approve) request.reason = reason(); }
          const receipt = await api('/v1/owner/candidates/' + candidate.taxon_id + '/' + (approve ? 'approve' : 'reject'), request);
          location.assign(href('operation', {operation: receipt.operation_id}));
        } catch (error) { control.disabled = false; if (error.code === 'sign_in_required') ui.showProblem(error); else problem.textContent = error.message + ' Check recent operations if the response was lost.'; }
      });
    }
    if (candidate.can_approve) {
      const label = el('label', '', 'check-label'), consent = el('input'); consent.type = 'checkbox';
      label.append(consent, document.createTextNode('I inspected the full plate, its text, anatomy and cited review. Make this artwork approved locally and eligible for existing publication.'));
      const approve = decision(true); approve.disabled = true; consent.addEventListener('change', () => { approve.disabled = !consent.checked; });
      content.append(label, approve, note('Approval copies this reviewed candidate into the local catalog. Published copies cannot be recalled. Replacing approved art later needs the deliberate operator migration workflow.'));
    }
    const form = el('form'), label = el('label', 'Why reject this candidate?'); label.htmlFor = 'candidate-reason';
    const reason = el('textarea'); reason.id = label.htmlFor; reason.maxLength = 2000; reason.required = true;
    const reject = decision(false, () => { if (!reason.value.trim()) throw new Error('Give a rejection reason.'); return reason.value; });
    form.append(label, reason, reject); content.append(form, note('Rejection preserves the artifact privately and keeps a hold. It does not start another model attempt.'), problem); main.append(content);
  } else if (area === 'operation') {
    base('Operation outcome', 'Inky saves this result on the controller. Reloading this page does not repeat the action.');
    main.append(link('Back to Manage', 'manage'));
    const content = el('section', '', 'journal-card'); main.append(content);
    let timer, stopped = false;
    stopScreen = () => { stopped = true; clearTimeout(timer); };
    async function update() {
      try {
        const receipt = await api('/v1/owner/operations/' + encodeURIComponent(query.get('operation') || ''));
        if (stopped || !content.isConnected) return;
        content.replaceChildren(el('h2', operationNames[receipt.kind] || 'Owner operation'), el('p', receipt.status));
        if (receipt.result) {
          if (receipt.result.message) content.append(el('p', receipt.result.message));
          for (const [key, value] of Object.entries(receipt.result)) {
            if (key === 'message') continue;
            if (key === 'providers') {
              for (const provider of value) content.append(el('p', (sourceNames[provider.name]?.[0] || provider.name) + ': ' + provider.status + ' · ' + provider.species_count + ' species'));
            } else if (['string', 'number', 'boolean'].includes(typeof value)) content.append(note(key.replaceAll('_', ' ') + ': ' + value));
          }
        }
        if (['pending', 'running'].includes(receipt.status)) timer = setTimeout(update, document.hidden ? 5000 : 1000);
        else {
          if (receipt.kind === 'community-preview') content.append(link('Review community selection', 'community', {}, 'primary-link'));
          if (receipt.result?.collection_id) content.append(link('Open saved collection', 'collection', {view: receipt.result.collection_id}, 'primary-link'));
          content.append(link('Check sources', 'sources'), link('Check plate work', 'work'), link('Check notifications', 'notifications'));
        }
      } catch (error) {
        if (error.code === 'sign_in_required') ui.showProblem(error);
        else if (!stopped) { content.replaceChildren(note(error.message), button('Check again', update)); }
      }
    }
    await update();
  } else if (area === 'imports') {
    stopScreen = await renderImports(ui, {base, link, note, kind: query.get('kind') || 'ebird-archive', journalRevision: data.revision});
  } else if (area === 'settings') {
    const source = query.get('source');
    if (source && !sourceNames[source]) throw new Error('Choose a supported source.');
    await renderSettings(ui, {base, link, note, sourceNames, source});
  } else if (['displays','integrations'].includes(area)) {
    await renderPresentation(ui, {base, link, note, data, area});
  } else if (['health','backups','advanced'].includes(area)) {
    base(area==='health'?'Health and help':area==='backups'?'Backups and upgrades':'Advanced tools');main.append(link('Back to Manage','manage'));
    function guide(title,path,text){const section=el('section','','journal-card'), anchor=el('a','Open '+title,'text-link');anchor.href='https://github.com/veteranbv/inky-bird-frame/blob/main/docs/'+path;anchor.target='_blank';anchor.rel='noopener noreferrer';section.append(el('h2',title),note(text),anchor);main.append(section);}
    if(area==='health'){
      const health=await api('/v1/owner/health');main.append(note('Installed version: '+health.version),note(health.notice));
      for(const check of health.checks)main.append(empty(check.name+' · '+check.status,check.detail));
      guide('Operations','operations.md','Run the controller and display doctor from their installed hosts. Check actual scheduler/service health and successful panel telemetry. This page does not run privileged commands or download raw logs.');
      main.append(link('Check Sources','sources'),link('Check plate work','work'),link('Check notification delivery','notifications'));
    }else if(area==='backups'){
      guide('Backup and restore','backup.md','Stop browser serving and scheduled writers first. Back up the authoritative configuration, complete private state, approved catalog and working data together. Private state includes credentials. A collection export is not a controller backup.');
      guide('Upgrade and migration','migration-v1.md','Existing deployments keep their source choices, generation policy, Saved birds and default wall route. Make a matching runtime/state backup before updating. Do not let a newer runtime overwrite your only rollback copy.');
      main.append(link('Configuration ownership','settings'));
    }else{
      guide('Plate repair','operations.md','Approved replacements, research resets and correction-source selection require the existing explicit operator migration and human review. Browser approval cannot replace approved art or bypass required factual/visual checks.');
      guide('Installation and services','installation.md','Host binding, native services, executable paths, certificates and package updates remain installer-owned. Browser settings cannot safely pretend to reconfigure a host.');
      guide('HTTPS and Authentik','browser-access.md','Optional single sign-on uses an explicit owner identity link, not automatic promotion by email or group. Never copy the identity-service password into Inky.');
    }
  } else if (area === 'manage') {
    base('Manage', 'One controller. The same sources, reviewed plates and work rules.');
    const sections = [['sources', 'Sources', 'Check all seven supported inputs and the shared observation window.'],
      ['work', 'Plate work', 'Keep stopped, waiting and runnable work distinct.'],
      ['displays', 'Displays', 'Check the last confirmed physical panel update.'],
      ['integrations', 'Home Assistant and Homepage', 'Choose scoped artwork and separate read-only status credentials.'],
      ['notifications', 'Notifications', 'Check delivery separately from bird discovery.'],
      ['settings', 'Settings and backups', 'Source choices, generation policy and configuration ownership.']];
    sections.push(['health','Health and help','Read-only application checks and host doctor guidance.'],['backups','Backups and upgrades','Protect private state and follow a deliberate migration.'],['advanced','Advanced tools','Explicit operator workflows for host setup and approved plate repair.']);
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
      main.append(operationAction('Refresh observations', 'refresh', 'Read all enabled sources now. Partial failures stay isolated. New live results can become eligible for your existing generation policy. This action does not start a model itself.'));
      for (const source of management.sources) {
        const [name, kind] = sourceNames[source.name]; const node = el('section', '', 'journal-card');
        const imported = ['ebird-archive', 'birdnet-analyzer'].includes(source.name);
        const state = !source.enabled ? 'Off' : source.status === 'ok'
          ? imported ? 'History read in last saved refresh' : source.species_count ? 'Last saved check succeeded' : 'Last saved check found no birds in this window'
          : source.status === 'error' ? 'Last saved check failed' : 'Not checked yet';
        node.append(el('h2', name), el('p', state, source.status === 'error' ? 'error' : 'connection'), note(kind));
        if (imported) node.append(note('Imported history is not a live connection.'));
        if (source.last_checked_at) node.append(note('Last saved check: '+new Date(source.last_checked_at).toLocaleString()));
        if (imported) node.append(link('Import or review ' + name + ' history', 'imports', {kind: source.name}, 'primary-link'));
        if (source.enabled && !management.work.complete) node.append(note('Discovery is ' + management.work.discovery_status + '. This is saved state, not a current connection test.'));
        if (source.unresolved_count) node.append(note(source.unresolved_count + ' unresolved species do not trigger generation.'));
        node.append(link('Set up ' + name, 'settings', {source: source.name}, 'primary-link'));
        main.append(node);
      }
    } else if (area === 'work') {
      main.append(note(management.generation_enabled ? 'Generation is enabled. Eligible work follows the scheduler and existing review rules.' : 'Generation is off. History, queued work and approved plates stay intact.'), link('Generation settings', 'settings'));
      if (!management.work.complete) main.append(el('p', 'Discovery is ' + management.work.discovery_status + '. Work counts may be incomplete.', 'notice'));
      const pending = management.work.in_review;
      main.append(el('h2', 'In review or recovery · ' + pending.candidates.length));
      if (pending.status !== 'ok') main.append(el('p', 'Pending inventory is incomplete or unavailable. Use the operator recovery guide; do not treat this count as complete.', 'notice'));
      for (const candidate of pending.candidates) main.append(candidate.inspect_available ? link('Inspect ' + candidate.common_name, 'candidate', {bird: candidate.taxon_id}, 'primary-link') : note(candidate.common_name + ': incomplete or unsafe pending state. Use operator recovery before retrying.'));
      const requested = byId.get(Number(query.get('bird')));
      if (requested && !requested.plate_ready && !pending.candidates.some(candidate => candidate.taxon_id === requested.taxon_id) && !management.work.needs_attention.includes(requested.taxon_id)) main.append(el('h2', 'Explicit recovery: ' + requested.common_name), operationAction('Review retry for ' + requested.common_name, 'retry', 'This bird may be outside the current live selection. Explicit retry preserves correction guidance and mandatory review, makes it durable, and can use AI allowance when generation is enabled. It does not run a model now.', requested.taxon_id));
      for (const [key, label] of [['can_run', 'Can run'], ['waiting', 'Waiting until retry'], ['needs_attention', 'Needs attention']]) {
        const ids = management.work[key], section = el('section', '', 'journal-card'); section.append(el('h2', label + ' · ' + ids.length));
        for (const id of ids) {
          section.append(link(byId.get(id)?.common_name || 'Taxon ' + id, 'bird', {bird: id}));
          const retry=management.work.retry_details?.find(item=>item.taxon_id===id), terminal=management.work.terminal_details?.find(item=>item.taxon_id===id);
          if(retry)section.append(note(retry.attempts+' recorded failed cycles. Next retry eligibility: '+new Date(retry.next_attempt_at).toLocaleString()));
          if(terminal)section.append(note('Retained hold: '+terminal.state.replaceAll('_',' ')+'. Use explicit recovery; this is not runnable work.'));
          if (key !== 'can_run') section.append(operationAction('Review retry for ' + (byId.get(id)?.common_name || 'Taxon ' + id), 'retry', 'Make this species eligible for a new attempt. Inky retains correction guidance, archives the previous attempt, and preserves mandatory review. Generation can use your AI allowance or incur costs when enabled. A retry does not promise a better result or an immediate run.', id));
        } main.append(section);
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
      main.append(note('Destination secrets stay on the controller. App-managed settings can change destinations; managed settings stay with the installer.'));
      main.append(link('Notification settings', 'settings'));
      if (state.enabled) main.append(operationAction('Send a test notification', 'notification-test', 'Send one clearly labeled test to each configured destination. This is a real external notification. The result reports failures separately.'));
      if (state.enabled && state.dead_letters) main.append(operationAction('Retry dead letters', 'notification-retry', 'Return failed messages to the existing outbox and request delivery. Destinations that already received a message are not sent it again.'));
    }
    const recent = await api('/v1/owner/operations');
    if (main.isConnected && recent.operations.length) {
      main.append(el('h2', 'Recent owner operations'));
      for (const receipt of recent.operations.slice().reverse()) main.append(link((operationNames[receipt.kind] || 'Operation') + ' · ' + receipt.status, 'operation', {operation: receipt.operation_id}));
    }
  } else { base('Page not found', 'Choose a place in your journal.'); }
}
