// Settings edit the controller's authoritative file. Secrets are write-only.
export async function renderSettings(ui, {base, link, note, sourceNames, source}) {
  const {element: el, button, api} = ui;
  const main = document.querySelector('#main');
  const settings = await api('/v1/owner/settings');
  base(source ? sourceNames[source][0] : 'Settings and backups', source
    ? sourceNames[source][1] : 'Choose how Inky runs. Installation and network settings stay with the installer.');
  if (source) main.append(link('Back to Sources', 'sources'));
  main.append(link('Back to Manage', 'manage'));
  const problem = el('p', '', 'error'); problem.setAttribute('role', 'alert');
  const privateNotice = note('HTTP does not encrypt settings or credentials in transit. Use a unique Inky password on a trusted home network. Use HTTPS for untrusted networks.');
  if (location.protocol === 'http:') main.append(privateNotice);
  if (source === 'birdbuddy') await birdBuddyAccount(ui, main, note);
  if (settings.restart_required) {
    main.append(el('h2', 'Finish switching configuration ownership'), note('Your file is now app-managed. Re-run the controller installer with this same file, then restart Inky. This replaces fixed timers with the application scheduler. Until then, browser edits stay locked. No source history or plates were removed.'));
    return;
  }
  if (!settings.editable) {
    main.append(el('h2', 'Managed by your installer'), note('Your TOML file and environment remain authoritative. Change the file, validate it, then re-run native setup or restart Docker. This page never edits a temporary copy that startup will replace.'));
    if (settings.revision) main.append(button('Preview switch to app-managed settings', () => {
      const preview = el('section', '', 'notice');
      preview.append(el('h2', 'Keep one configuration file'), note('Inky will back up this private file, then mark it app-managed. Your source choices, generation policy, credentials and history stay unchanged. Future browser saves update this same file. Environment credentials stay locked.'),
        note('You must re-run native setup and restart the service after this change. Docker will preserve this app-managed file on restart. Host, HTTPS and identity settings remain installer-owned. To undo adoption, stop Inky and restore the matching previous file, then run setup again.'),
        button('Confirm switch', async event => {
          const control = event.currentTarget; control.disabled = true;
          try { await api('/v1/owner/settings', {revision: settings.revision, changes: {}, adopt: true}); location.reload(); }
          catch (error) { problem.textContent = error.message; control.disabled = false; }
        }), button('Keep managed settings', () => preview.remove()));
      main.append(preview, problem); preview.querySelector('button').focus();
    }));
    return;
  }
  const form = el('form'), fields = settings.discovery;
  const changes = {discovery: {}, controller: {}, notifications: {}};
  const read = [];
  function section(title, text = '') {
    const fieldset = el('fieldset'); fieldset.append(el('legend', title));
    if (text) fieldset.append(note(text)); form.append(fieldset); return fieldset;
  }
  function input(parent, key, label, value, {type = 'text', hint = '', secret = false, external = false, target = 'discovery'} = {}) {
    const field = el('input'), caption = el('label', label);
    field.id = 'setting-' + key; caption.htmlFor = field.id;
    field.type = secret ? 'password' : type; field.value = value ?? '';
    field.autocomplete = secret ? 'new-password' : 'off'; field.spellcheck = false;
    field.disabled = external;
    if (type === 'number') { field.step = ['latitude', 'longitude'].includes(key) ? 'any' : '1'; }
    if (external) field.placeholder = 'Managed by the installer environment';
    else if (secret) field.placeholder = settings.credentials[key]?.configured ? 'Already set. Leave empty to keep it.' : 'Not set';
    parent.append(caption, field); if (hint) parent.append(note(hint));
    read.push(() => {
      if (external || secret && !field.value) return;
      changes[target][key] = field.value.trim() === '' ? null : type === 'number' ? Number(field.value) : field.value;
    });
    return field;
  }
  function checkbox(parent, label, checked, readValue) {
    const row = el('label', '', 'check-label'), field = el('input'); field.type = 'checkbox'; field.checked = checked;
    row.append(field, document.createTextNode(label)); parent.append(row); read.push(() => readValue(field.checked)); return field;
  }
  if (source) {
    const group = section('Use this source', 'Enabling a source can put newly observed birds into the live selection. It does not download media. Automatic plate work follows your generation setting.');
    checkbox(group, 'Include ' + sourceNames[source][0] + ' in scheduled discovery', fields.sources.includes(source), checked => {
      changes.discovery.sources = Object.keys(sourceNames).filter(key => key === source ? checked : fields.sources.includes(key));
    });
    if (['inaturalist', 'ebird'].includes(source)) main.append(note('Location and the live window are shared. Open Settings to change them.'));
    const secrets = {ebird: ['ebird_api_key', 'eBird API key'], birdweather: ['birdweather_token', 'BirdWeather station token']};
    if (secrets[source]) {
      const [key, name] = secrets[source];
      input(section('Connection'), key, name, '', {secret: true, external: settings.credentials[key]?.external});
    }
    if (source === 'birdnet-go') input(section('Connection', 'Use a server address that this controller can reach. Do not use an HTTPS login page in place of the BirdNET-Go API.'), 'birdnet_go_url', 'BirdNET-Go base URL', fields.birdnet_go_url, {type: 'url'});
    if (source === 'birdbuddy') {
      const group = section('Manual records', 'Bird Buddy manual sightings can be account-wide and may not belong to your selected feeder. They are separate from automatic feeder detections.');
      checkbox(group, 'Include manually entered sightings', fields.birdbuddy_include_manual_sightings, checked => { changes.discovery.birdbuddy_include_manual_sightings = checked; });
    }
    if (['ebird-archive', 'birdnet-analyzer'].includes(source)) form.append(note('An import is retained history, not a live connection. Enabling it selects records within the shared window; it does not request a new file automatically.'));
  } else {
    const locationGroup = section('Location for community reports', 'Choose one form. This location applies to iNaturalist and nearby eBird only, not your stations or imported trips.');
    const mode = el('select'); mode.setAttribute('aria-label', 'Location format');
    for (const [value, name] of [['none', 'No community location'], ['coordinates', 'Latitude and longitude'], ['zip', 'US ZIP code'], ['postal', 'International postal code']]) {
      const option = el('option', name); option.value = value; mode.append(option);
    }
    mode.value = fields.latitude !== undefined ? 'coordinates' : fields.zip_code ? 'zip' : fields.postal_code ? 'postal' : 'none';
    locationGroup.append(mode);
    const rows = new Map();
    for (const [key, title, type, group] of [['latitude', 'Latitude', 'number', 'coordinates'], ['longitude', 'Longitude', 'number', 'coordinates'], ['zip_code', 'US ZIP code', 'text', 'zip'], ['postal_code', 'Postal code', 'text', 'postal'], ['country_code', 'Two-letter country code', 'text', 'postal']]) {
      const row = el('div'); locationGroup.append(row); input(row, key, title, fields[key], {type}); rows.set(key, [row, group]);
    }
    const geocoder = el('div'); locationGroup.append(geocoder);
    input(geocoder, 'geoapify_api_key', 'Geoapify API key', '', {secret: true, external: settings.credentials.geoapify_api_key.external});
    function updateLocation() { for (const [row, group] of rows.values()) row.hidden = group !== mode.value; geocoder.hidden = mode.value !== 'postal'; }
    mode.addEventListener('change', updateLocation); updateLocation();
    read.push(() => {
      for (const [key, [, group]] of rows) if (group !== mode.value) changes.discovery[key] = null;
      if (mode.value !== 'postal' && !settings.credentials.geoapify_api_key.external) changes.discovery.geoapify_api_key = null;
    });
    const selection = section('Shared live selection', 'Collections keep their own fixed membership. These choices control scheduled observation refreshes. eBird nearby supports up to 30 days and 50 km.');
    const window = el('select'); window.setAttribute('aria-label', 'Observation window');
    for (const key of ['last-day', 'last-week', 'last-30-days', 'last-year', 'all-time']) {
      const option = el('option', key.replaceAll('-', ' ')); option.value = key; window.append(option);
    }
    window.value = fields.window; selection.append(window); read.push(() => { changes.discovery.window = window.value; });
    input(selection, 'radius_km', 'Radius in kilometers', fields.radius_km, {type: 'number'});
    input(selection, 'species_limit', 'Live species limit', fields.species_limit, {type: 'number', hint: 'This limits live discovery, not the membership of a saved trip. BirdWeather supports at most 100.'});
    const generation = section('Plate generation', 'Approved art is free to browse. First install and sign in to Codex through the operator setup, then run the controller doctor. Creating missing plates uses that AI account and its allowance or costs. Inky keeps sourced review and retry limits. Disabling generation leaves history and queued work intact. An attempt already running can finish.');
    checkbox(generation, 'Enable scheduled plate generation', settings.generation_enabled, checked => { changes.controller.generation_enabled = checked; });
    const consent = checkbox(generation, 'I understand that enabling generation can start AI work at the next scheduled cycle.', false, () => {});
    read.push(() => { if (!settings.generation_enabled && changes.controller.generation_enabled && !consent.checked) throw new Error('Confirm AI use before enabling generation.'); });
    const notifications = section('Notifications', 'Secrets are never returned. Leave an existing destination URL empty to keep it. Changing its name requires its URL again. Removing a row removes that destination after confirmation.');
    checkbox(notifications, 'Enable notifications', settings.notifications.enabled, checked => { changes.notifications.enabled = checked; });
    const destinationRows = el('div'); notifications.append(destinationRows);
    const external = settings.notifications.destinations.some(item => item.external);
    const destinationReaders = [];
    function destination(saved = {}) {
      const row = el('fieldset'); row.append(el('legend', 'Destination'));
      const name = el('input'); name.value = saved.name || ''; name.setAttribute('aria-label', 'Destination name');
      const url = el('input'); url.type = 'password'; url.autocomplete = 'new-password'; url.setAttribute('aria-label', 'Private notification URL'); url.placeholder = saved.name ? 'Already set. Leave empty to keep.' : 'Apprise notification URL';
      row.append(name, url);
      const events = ['discovery', 'generation_approved', 'terminal_error', 'degraded', 'recovered', 'publication_error', 'publication_recovered', 'display_stale', 'display_recovered'];
      const checked = [];
      for (const event of events) {
        const label = el('label', '', 'check-label'), field = el('input'); field.type = 'checkbox'; field.checked = !saved.events || saved.events.includes(event);
        label.append(field, document.createTextNode(event.replaceAll('_', ' '))); row.append(label); checked.push([event, field]);
      }
      row.append(button('Remove destination', () => row.remove())); destinationRows.append(row);
      destinationReaders.push(() => row.isConnected ? {name: name.value.trim(), ...(url.value ? {url: url.value} : {}), events: checked.filter(([, field]) => field.checked).map(([key]) => key)} : null);
    }
    if (external) notifications.append(note('Destination credentials are managed by the environment. Change them through the installer.'));
    else {
      for (const saved of settings.notifications.destinations) destination(saved);
      notifications.append(button('Add destination', () => destination()));
      read.push(() => { changes.notifications.destinations = destinationReaders.map(get => get()).filter(Boolean); });
    }
    read.push(() => { changes.confirm_generation = consent.checked; });
  }
  const review = el('section', '', 'notice'); review.hidden = true;
  const submit = el('button', 'Review changes'); submit.type = 'submit';
  form.append(submit, problem, review); main.append(form);
  form.addEventListener('input', () => { review.hidden = true; });
  form.addEventListener('submit', event => {
    event.preventDefault(); problem.textContent = ''; review.replaceChildren();
    try {
      changes.discovery = {}; changes.controller = {}; changes.notifications = {};
      for (const get of read) get();
      const consent = changes.confirm_generation || false; delete changes.confirm_generation;
      const summary = [];
      if (source) summary.push((changes.discovery.sources.includes(source) ? 'Use ' : 'Do not use ') + sourceNames[source][0] + ' in scheduled discovery.');
      else summary.push('Live window: ' + changes.discovery.window.replaceAll('-', ' ') + '.', 'Generation: ' + (changes.controller.generation_enabled ? 'on' : 'off') + '.', 'Notifications: ' + (changes.notifications.enabled ? 'on' : 'off') + '.');
      review.append(el('h2', 'Save these choices?'), ...summary.map(note), note('Inky validates the whole file before saving. A private previous-file backup is kept. The scheduler reads changes on its next check. Saving a new source does not run a connection test.'),
        button('Save settings', async event => {
          const control = event.currentTarget; control.disabled = true;
          try {
            await api('/v1/owner/settings', {revision: settings.revision, changes, confirm_generation: consent});
            for (const field of form.querySelectorAll('input[type="password"]')) field.value = '';
            location.reload();
          } catch (error) { problem.textContent = error.message; control.disabled = false; }
        }), button('Keep editing', () => { review.hidden = true; submit.focus(); }));
      review.hidden = false; review.querySelector('button').focus();
    } catch (error) { problem.textContent = error.message; }
  });
}

async function birdBuddyAccount(ui, main, note) {
  const {api, element: el, button} = ui;
  const response = await api('/v1/owner/birdbuddy'), state = response.state;
  const section = el('section', '', 'journal-card');
  section.append(el('h2', 'Bird Buddy account'), note('Use a dedicated email/password guest account. Bird Buddy’s private API is unsupported and can change without notice. You must obtain and maintain Bird Buddy’s permission. Inky cannot grant or verify it.'));
  const terms = el('a', 'Read Bird Buddy’s private-API restrictions', 'text-link'); terms.href = 'https://mybirdbuddy.com/app-eula/'; terms.target = '_blank'; terms.rel = 'noopener noreferrer';
  const guests = el('a', 'Set up a guest feeder member', 'text-link'); guests.href = 'https://support.mybirdbuddy.com/hc/en-us/articles/4404431992337-Adding-guest-Feeder-Members-to-your-Birdbuddy'; guests.target = '_blank'; guests.rel = 'noopener noreferrer';
  section.append(terms, guests);
  const problem = el('p', '', 'error'); problem.setAttribute('role', 'alert');
  if (state.authenticated) {
    section.append(el('p', 'Saved login for ' + state.feeder.name), note('Role: ' + state.feeder.role + '. Saved credentials are present; this is not a live token check. Use Refresh observations on Sources to check the enabled provider.'));
    if (!state.least_privilege_guest) section.append(el('p', 'This is an owner account. A dedicated guest account is the supported least-privilege setup. Reauthorize with a guest account.', 'notice'));
    if (state.history) {
      section.append(note('Last successful synchronization: ' + new Date(state.history.last_successful_sync_at).toLocaleString()), note('Retained postcards: ' + state.history.retained_postcards + '. Manual sightings: ' + (state.history.manual_sightings_included ? 'included by your explicit setting' : 'excluded') + '.'));
    }
    section.append(button('Remove local Bird Buddy login', async event => {
      if (!confirm('Remove this installation’s Bird Buddy login and permission attestation? Retained detections and plates stay. This does not revoke the token at Bird Buddy.')) return;
      const control = event.currentTarget; control.disabled = true;
      try { await api('/v1/owner/birdbuddy/logout', {confirm: true}); location.reload(); }
      catch (error) { control.disabled = false; if (error.code === 'sign_in_required') ui.showProblem(error); else problem.textContent = error.message; }
    }));
  } else section.append(note(state.history_present ? 'No saved login. Earlier private detection history is retained.' : 'Not signed in. No private detection history is available yet.'));
  if (!response.secure_bootstrap) {
    section.append(el('p', 'Bird Buddy sign-in needs HTTPS, or a browser on the controller using its loopback address. Ordinary home-network HTTP cannot protect this guest password. The documented CLI login remains available.', 'notice'), problem); main.append(section); return;
  }
  const form = el('form'), attestation = el('label', '', 'check-label'), confirmed = el('input'); confirmed.type = 'checkbox';
  attestation.append(confirmed, document.createTextNode('I confirm that Bird Buddy has authorized automated API access for this account.'));
  form.append(attestation);
  function credential(title, type, id) {
    const label = el('label', title), input = el('input'); input.id = id; input.type = type; input.autocomplete = 'off'; input.required = true; input.disabled = true;
    input.maxLength = type === 'email' ? 320 : 4096; label.htmlFor = id; form.append(label, input); return input;
  }
  const email = credential('Dedicated guest email', 'email', 'birdbuddy-email'), password = credential('Guest password (used only for this sign-in)', 'password', 'birdbuddy-password');
  const feeders = el('div'), submit = el('button', state.authenticated ? 'Replace saved guest login' : 'Sign in to Bird Buddy'); submit.type = 'submit'; submit.disabled = true;
  let choice = null;
  confirmed.addEventListener('change', () => {
    email.disabled = password.disabled = submit.disabled = !confirmed.checked;
    if (!confirmed.checked) { email.value = password.value = ''; feeders.replaceChildren(); choice = null; }
  });
  for (const input of [email, password]) input.addEventListener('input', () => { feeders.replaceChildren(); choice = null; submit.textContent = state.authenticated ? 'Replace saved guest login' : 'Sign in to Bird Buddy'; });
  form.append(feeders, note('Inky keeps the rotating refresh token, feeder choice and attestation date. It does not save this email or password. Choose the feeder explicitly if the guest can access more than one.'), submit);
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (!confirmed.checked) return;
    submit.disabled = true; problem.textContent = '';
    try {
      const body = {confirm: true, email: email.value, password: password.value};
      if (choice) { if (!choice.value) throw new Error('Choose the feeder for this installation.'); body.feeder_id = choice.value; }
      const result = await api('/v1/owner/birdbuddy/login', body);
      if (!form.isConnected) return;
      if (result.choose_feeder) {
        const label = el('label', 'Choose this installation’s feeder'); label.htmlFor = 'birdbuddy-feeder'; choice = el('select'); choice.id = label.htmlFor; choice.required = true;
        const empty = el('option', 'Choose a feeder'); empty.value = ''; choice.append(empty);
        for (const feeder of result.feeders) { const option = el('option', feeder.name + ' · ' + feeder.role); option.value = feeder.feeder_id; choice.append(option); }
        feeders.replaceChildren(label, choice); submit.textContent = 'Use the selected feeder'; choice.focus();
      } else { email.value = password.value = ''; location.reload(); }
    } catch (error) { if (error.code === 'sign_in_required') { email.value = password.value = ''; ui.showProblem(error); } else problem.textContent = error.message; }
    finally { submit.disabled = !confirmed.checked; }
  });
  section.append(form, problem); main.append(section);
}
