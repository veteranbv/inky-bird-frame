// Raw files live only in controller staging and the browser's file input.
// History confirmation and trip saving are separate, recoverable steps.
export async function renderImports(ui, { base, link, note, kind }) {
  const { element: el, button, api, upload } = ui;
  if (!['ebird-archive', 'birdnet-analyzer'].includes(kind))
    throw new Error('Choose a supported history source.');
  base(
    kind === 'ebird-archive' ? 'Your eBird history' : 'Analyzer history',
    'Check the file first. You choose when history changes.',
  );
  const main = document.querySelector('#main'),
    content = el('section', '', 'journal-card');
  const steps = el(
    'p',
    kind === 'ebird-archive'
      ? 'File → Check → History → Checklists → Save'
      : 'File → Check → History',
    'hint',
  );
  main.append(steps, content, link('Back to sources', 'sources'));
  let stopped = false,
    timer,
    transfer = null;
  const randomId = () =>
    Array.from(crypto.getRandomValues(new Uint8Array(16)), (item) =>
      item.toString(16).padStart(2, '0'),
    ).join('');
  const problem = () => {
    const node = el('p', '', 'error');
    node.setAttribute('role', 'alert');
    return node;
  };
  function field(form, title, type, value = '') {
    const label = el('label', title),
      input = el('input');
    input.id = 'import-' + type + '-' + form.children.length;
    input.type = type;
    input.value = value;
    label.htmlFor = input.id;
    form.append(label, input);
    return input;
  }
  function fail(node, error) {
    if (error.code === 'sign_in_required') ui.showProblem(error);
    else node.textContent = error.message;
  }
  function operation(label, action, uploadId, extra, onSuccess = load) {
    const section = el('div'),
      error = problem();
    const key = randomId();
    let request = null;
    const start = button(label, async (event) => {
      const control = event.currentTarget;
      control.disabled = true;
      error.textContent = '';
      try {
        if (!request) {
          const settings = await api('/v1/owner/settings');
          request = {
            upload_id: uploadId,
            key,
            revision: settings.revision,
            confirm: true,
            ...(await extra()),
          };
        }
        const receipt = await api('/v1/owner/imports/' + action, request);
        if (!stopped) await watch(receipt.operation_id, onSuccess);
      } catch (failure) {
        control.disabled = false;
        fail(error, failure);
      }
    });
    section.append(start, error);
    return section;
  }
  async function watch(id, done = load) {
    clearTimeout(timer);
    const receipt = await api('/v1/owner/operations/' + encodeURIComponent(id));
    if (stopped || !content.isConnected) return;
    content.replaceChildren(
      el('h2', 'Working on this step'),
      note(receipt.status),
      link('Saved operation outcome', 'operation', { operation: id }),
    );
    if (['pending', 'running'].includes(receipt.status)) {
      timer = setTimeout(
        () =>
          watch(id, done).catch((error) => {
            if (!stopped) {
              const alert = problem();
              fail(alert, error);
              content.append(
                alert,
                button('Check this operation again', () => watch(id, done)),
              );
            }
          }),
        document.hidden ? 5000 : 1000,
      );
      return;
    }
    if (receipt.status === 'succeeded') {
      if (receipt.result?.collection_id) {
        location.assign(
          '/app?page=collection&view=' + encodeURIComponent(receipt.result.collection_id),
        );
        return;
      }
      await done();
      return;
    }
    content.replaceChildren(
      el(
        'h2',
        receipt.status === 'interrupted' ? 'This step was interrupted' : 'This step did not finish',
      ),
      note(receipt.result?.message || 'Inspect saved state before an explicit retry.'),
      button('Check saved state', load),
      link('See the saved outcome', 'operation', { operation: id }),
    );
  }
  async function load() {
    const state = (await api('/v1/owner/imports')).preview;
    if (stopped || !content.isConnected) return;
    content.replaceChildren();
    if (!state) {
      chooseFile();
      return;
    }
    if (state.kind !== kind) {
      content.append(
        el('h2', 'Another history preview is open'),
        link('Resume that preview', 'imports', { kind: state.kind }),
        cancel(state),
      );
      return;
    }
    content.append(
      note('Private source file expires: ' + new Date(state.expires_at).toLocaleString()),
    );
    if (Date.parse(state.expires_at) <= Date.now() || state.status === 'receiving') {
      content.append(
        el('h2', state.status === 'receiving' ? 'Upload did not finish' : 'Preview expired'),
        note(
          'Confirmed history and saved trips stay intact. Cancel this temporary file and select the original again.',
        ),
        cancel(state),
      );
      return;
    }
    if (state.status === 'uploaded') {
      content.append(
        el('h2', '2. Check the file'),
        note(
          'Inky validates the complete file and compares it with retained history. This step does not change history.',
        ),
        operation('Check this file', 'import-preview', state.upload_id, () => ({})),
        cancel(state),
      );
      return;
    }
    if (state.status === 'committed') {
      content.append(
        el('h2', 'History is confirmed'),
        note(
          'The confirmed history step is complete. A trip has not been saved unless you see it in Collections.',
        ),
      );
      if (kind === 'birdnet-analyzer') {
        content.append(
          note(
            'Import does not enable this source, refresh observations, queue a plate or change the wall.',
          ),
          cancel(state, 'Finish'),
          link('Analyzer settings', 'settings', { source: kind }),
        );
        return;
      }
      if (state.trip) {
        reviewTrip(state);
        return;
      }
      chooseChecklists(state);
      return;
    }
    const preview = state.preview;
    content.append(el('h2', '3. Review history changes'));
    const labels = {
      rows: 'Source rows',
      checklists: 'Checklists',
      species: 'Species names',
      duplicate_rows: 'Duplicate rows',
      added_checklists: 'New checklists',
      updated_checklists: 'Changed checklists',
      removed_checklists: 'Removed checklists',
      unchanged_checklists: 'Unchanged checklists',
      imported: 'New detections',
      updated: 'Changed detections',
      duplicates: 'Unchanged detections',
      dated: 'Dated detections',
      undated: 'Undated detections',
    };
    const counts = el('dl', '', 'measurements');
    for (const [key, label] of Object.entries(labels))
      if (key in preview.stats)
        counts.append(el('dt', label), el('dd', String(preview.stats[key])));
    content.append(
      counts,
      note(preview.notice),
      note(
        preview.impact.live_source_enabled
          ? 'This source already participates in scheduled discovery.'
          : 'This source is off. Importing will not turn it on.',
      ),
      note(
        preview.generation_enabled
          ? 'Automatic generation is enabled. New eligible species can use your AI allowance after refresh.'
          : 'Generation is off. This import will not turn it on.',
      ),
    );
    if (preview.impact.entering_window)
      content.append(
        note(
          'Source names entering the live date window: ' +
            preview.impact.entering_window.length +
            '. Leaving: ' +
            preview.impact.leaving_window.length +
            '. Provider limits and cross-source matching still apply.',
        ),
      );
    if (preview.impact.undated_notice) content.append(note(preview.impact.undated_notice));
    let acknowledge = null;
    if (preview.stats.removed_checklists || preview.impact.affected_collections?.length) {
      const label = el('label', '', 'check-label');
      acknowledge = el('input');
      acknowledge.type = 'checkbox';
      label.append(
        acknowledge,
        document.createTextNode(
          'I reviewed removed checklists and changed collection evidence. Keep my curated groups and mark changed evidence.',
        ),
      );
      for (const name of preview.impact.affected_collections || [])
        content.append(note('Evidence changes in: ' + name));
      content.append(label);
    }
    content.append(
      operation(
        preview.unchanged ? 'No history changes · continue' : 'Confirm history update',
        'import-commit',
        state.upload_id,
        () => {
          if (acknowledge && !acknowledge.checked)
            throw new Error('Confirm that you reviewed removals and changed evidence.');
          return {
            preview_hash: preview.preview_hash,
            allow_reduction: acknowledge?.checked || false,
          };
        },
      ),
      operation('Check this file again', 'import-preview', state.upload_id, () => ({})),
      cancel(state),
    );
  }
  function cancel(state, label = 'Cancel preview') {
    return button(label, async (event) => {
      const control = event.currentTarget;
      control.disabled = true;
      try {
        await api('/v1/owner/imports/cancel', { upload_id: state.upload_id });
        await load();
      } catch (error) {
        control.disabled = false;
        const alert = problem();
        fail(alert, error);
        content.append(alert);
      }
    });
  }
  function chooseFile() {
    if (kind === 'ebird-archive') {
      const error = problem();
      content.append(
        button('Use retained history without an upload', async (event) => {
          const control = event.currentTarget;
          control.disabled = true;
          try {
            await api('/v1/owner/imports/retained', {});
            await load();
          } catch (failure) {
            control.disabled = false;
            fail(error, failure);
          }
        }),
        note(
          'Choose from retained checklist dates and species. Upload the original archive if you need location labels or updated history.',
        ),
        error,
      );
    }
    content.append(
      el('h2', '1. Choose the complete source file'),
      note(
        kind === 'ebird-archive'
          ? 'Use the official eBird Download My Data ZIP or CSV. Do not trim it to one trip: the archive replaces retained history.'
          : 'Use BirdNET Analyzer CSV output. A known recording date applies to this batch. Leave it blank if the date is unknown.',
      ),
    );
    const form = el('form'),
      file = field(form, 'History file', 'file');
    file.accept = kind === 'ebird-archive' ? '.zip,.csv' : '.csv';
    file.required = true;
    const observed =
      kind === 'birdnet-analyzer' ? field(form, 'Known recording date (optional)', 'date') : null;
    const error = problem(),
      send = el('button', 'Upload for checking');
    send.type = 'submit';
    const cancel = button('Stop upload', () => transfer?.abort());
    cancel.hidden = true;
    const progress = el('p', '', 'hint');
    progress.setAttribute('role', 'status');
    form.append(
      note(
        'Up to 512 MiB on the wire; eBird ZIP contents must also decode to no more than 512 MiB. Inky requires 2 GiB free before upload. Files stay private and expire after the configured preview period.',
      ),
      send,
      cancel,
      progress,
      error,
    );
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const source = file.files[0];
      if (!source) return;
      if (!source.size || source.size > 512 * 1024 * 1024) {
        error.textContent = 'Choose a nonempty file no larger than 512 MiB.';
        return;
      }
      send.disabled = true;
      file.disabled = true;
      if (observed) observed.disabled = true;
      cancel.hidden = false;
      transfer = new AbortController();
      progress.textContent = 'Uploading privately. No history has changed. Keep this page open.';
      try {
        const suffix = observed?.value ? '?observed_on=' + encodeURIComponent(observed.value) : '';
        await upload('/v1/owner/imports/upload/' + kind + suffix, source, transfer.signal);
        if (!stopped) await load();
      } catch (failure) {
        if (!stopped) {
          progress.textContent =
            'No history changed. If the response was lost, check the saved upload before sending again.';
          fail(error, failure);
          form.append(button('Check saved upload', load));
        }
      } finally {
        transfer = null;
        send.disabled = false;
        file.disabled = false;
        if (observed) observed.disabled = false;
        cancel.hidden = true;
      }
    });
    content.append(form);
  }
  function chooseChecklists(state) {
    content.append(
      el('h2', '4. Choose your checklists'),
      note(
        'Calendar dates include both endpoints. Location names come only from this private source file and are not copied into the saved trip.',
      ),
    );
    const draft = state.draft || {};
    const form = el('form'),
      start = field(
        form,
        'Trip start date',
        'date',
        draft.start_date || state.preview.stats.earliest_observed_on,
      ),
      end = field(
        form,
        'Trip end date',
        'date',
        draft.end_date || state.preview.stats.latest_observed_on,
      ),
      name = field(form, 'Trip name', 'text', draft.label || '');
    start.required = end.required = name.required = true;
    name.maxLength = 128;
    const next = el('button', 'Find checklists');
    next.type = 'submit';
    const error = problem(),
      choices = el('div');
    form.append(next, error, choices);
    content.append(form, cancel(state, 'Finish without a trip'));
    let selection = 0;
    for (const input of [start, end, name])
      input.addEventListener('input', () => {
        selection++;
        choices.replaceChildren();
      });
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const pending = ++selection;
      next.disabled = true;
      try {
        const result = await api('/v1/owner/imports/checklists', {
          upload_id: state.upload_id,
          start_date: start.value,
          end_date: end.value,
        });
        if (stopped || !form.isConnected || pending !== selection) return;
        const selected = new Set(),
          fields = el('fieldset');
        fields.append(el('legend', 'Checklists for this trip'));
        for (const record of result.checklists) {
          const label = el('label', '', 'check-label'),
            input = el('input');
          input.type = 'checkbox';
          if (draft.checklist_keys?.includes(record.key)) {
            input.checked = true;
            selected.add(record.key);
          }
          input.addEventListener('change', () =>
            input.checked ? selected.add(record.key) : selected.delete(record.key),
          );
          label.append(
            input,
            document.createTextNode(
              record.observed_on +
                ' · ' +
                (record.location || 'Location unavailable') +
                ' · ' +
                record.species_count +
                ' species',
            ),
          );
          fields.append(label);
        }
        const collection_id = draft.collection_id || randomId();
        choices.replaceChildren(
          fields,
          note(
            result.checklists.length +
              ' checklists. ' +
              (draft.checklist_keys
                ? 'Previous choices are restored where they still exist. Review them against the current history.'
                : 'None are selected automatically.'),
          ),
          operation('Review this trip', 'trip-preview', state.upload_id, async () => {
            if (!selected.size) throw new Error('Choose at least one checklist.');
            const current = await api('/v1/owner/journal');
            return {
              choices: {
                history_revision: result.history_revision,
                checklist_keys: [...selected],
                start_date: result.start_date,
                end_date: result.end_date,
                label: name.value,
                collection_id,
                revision: current.revision,
              },
            };
          }),
        );
      } catch (failure) {
        fail(error, failure);
      } finally {
        next.disabled = false;
      }
    });
  }
  function reviewTrip(state) {
    const preview = state.trip.preview;
    content.append(
      el('h2', '5. Save your trip'),
      el('p', preview.label),
      note(
        preview.matched +
          ' matched species · ' +
          preview.ready +
          ' plates ready · ' +
          preview.unresolved.length +
          ' unresolved',
      ),
      note(preview.notice),
    );
    for (const member of preview.unresolved)
      content.append(
        note(
          'Unresolved: ' +
            (member.scientific_name || member.common_name) +
            '. No generation will be requested for it.',
        ),
      );
    content.append(
      operation('Save this trip', 'trip-save', state.upload_id, () => ({
        preview_hash: preview.preview_hash,
      })),
      button('Edit trip selection', async () => {
        await api('/v1/owner/imports/trip-edit', { upload_id: state.upload_id });
        await load();
      }),
      cancel(state, 'Finish without saving this trip'),
      link('Check saved collections', 'collections'),
    );
  }
  const recent = await api('/v1/owner/operations');
  const active = recent.operations.find(
    (receipt) =>
      ['import-preview', 'import-commit', 'trip-preview', 'trip-save'].includes(receipt.kind) &&
      ['pending', 'running'].includes(receipt.status),
  );
  if (active) await watch(active.operation_id);
  else await load();
  return () => {
    stopped = true;
    clearTimeout(timer);
    transfer?.abort();
  };
}
