const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn, execFileSync } = require('node:child_process');
const playwright = require(process.env.INKY_QA_PLAYWRIGHT || 'playwright');
const engine = process.env.INKY_QA_BROWSER || 'chromium';
assert(['chromium', 'firefox', 'webkit'].includes(engine));
const root = path.resolve(__dirname, '../..');
const python = process.env.INKY_QA_PYTHON || path.join(root, '.venv/bin/python');
const username = 'synthetic-owner';
const password = require('node:crypto').randomBytes(32).toString('base64url');
const fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'inky-history-browser-'));
const origin = 'http://127.0.0.1:18796',
  configPath = path.join(fixture, 'config.toml');
const publicCatalog = JSON.parse(fs.readFileSync(root + '/catalog/index.json')).species;
const cardinal = publicCatalog.find((bird) => bird.common_name === 'Northern Cardinal');
const robin = publicCatalog.find((bird) => bird.common_name === 'American Robin');
fs.mkdirSync(path.join(fixture, 'catalog/species'), { recursive: true });
fs.cpSync(
  path.join(root, 'catalog', path.dirname(cardinal.portrait_path)),
  path.join(fixture, 'catalog', path.dirname(cardinal.portrait_path)),
  { recursive: true },
);
fs.writeFileSync(
  path.join(fixture, 'catalog/index.json'),
  JSON.stringify({ schema_version: 1, generated_at: cardinal.approved_at, species: [cardinal] }),
);
execFileSync(
  python,
  [
    '-c',
    `import csv,json,tomllib
from pathlib import Path
from inky_bird_frame.settings import initialize_config,_toml
from inky_bird_frame.ebird_archive import REQUIRED_COLUMNS
root=Path(${JSON.stringify(fixture)})
initialize_config(root/'config.toml')
p=root/'config.toml'
raw=tomllib.loads(p.read_text());raw['controller'].update(catalog_dir=str(root/'catalog'),port=18796,bind_host='127.0.0.1');p.write_bytes(_toml(raw))
birds=json.loads(${JSON.stringify(JSON.stringify([cardinal, robin]))})
with (root/'archive.csv').open('w',newline='') as handle:
 writer=csv.DictWriter(handle,fieldnames=REQUIRED_COLUMNS);writer.writeheader()
 for n,bird in enumerate(birds):
  row=dict.fromkeys(REQUIRED_COLUMNS,'');row.update({'Submission ID':f'S-SYNTHETIC-{n}','Date':['2001-03-01','2001-03-02'][n],'Location':f'Fixture garden {n}','Scientific Name':bird['scientific_name'],'Common Name':bird['common_name']});writer.writerow(row)
state=root/'var/controller';state.mkdir(parents=True)
cache={'schema_version':1,'entries':{bird['scientific_name']:{'taxon_id':bird['taxon_id'],'common_name':bird['common_name'],'scientific_name':bird['scientific_name']} for bird in birds}}
(state/'ebird-archive-taxonomy-crosswalk.json').write_text(json.dumps(cache))
from PIL import Image,ImageDraw
from inky_bird_frame.birds import BirdSpecies
from inky_bird_frame.catalog import write_candidate_manifest
from inky_bird_frame.models import SpeciesProfileData,SourceLink,QualityReview
links=[SourceLink(title='Fixture source one',url='https://first.example/bird'),SourceLink(title='Fixture source two',url='https://second.example/bird')]
for identifier in [9999991,9999992]:
 bird=BirdSpecies(identifier,'Synthetic QA candidate '+str(identifier),'Fixture candidate '+str(identifier),1,'fixture')
 folder=state/'pending'/f'{identifier}-synthetic-qa-candidate-{identifier}';folder.mkdir(parents=True)
 for name,size in [('portrait.png',(1200,1600)),('display.png',(1600,1200))]:
  image=Image.new('RGB',size,'white');ImageDraw.Draw(image).text((80,100),'SYNTHETIC QA CANDIDATE',fill='black',font_size=50);image.save(folder/name)
 profile=SpeciesProfileData(taxon_id=identifier,common_name=bird.common_name,scientific_name=bird.scientific_name,family='Fixture family',measurements={'length':'10 cm','wingspan':'20 cm','weight':'30 g'},field_marks=['One','Two','Three','Four'],habitat='Fixture habitat',behavior='Fixture behavior',palette=['white','gray','black'],sources=links)
 write_candidate_manifest(folder,bird,profile,[],QualityReview(True,5,5,5,5,True,(),tuple(links)),generator='fixture',prompt_version='fixture')
`,
  ],
  { cwd: root },
);
const log = fs.openSync(path.join(fixture, 'server.log'), 'a', 0o600);
const server = spawn(
  python,
  [
    '-c',
    `from pathlib import Path
import uvicorn
from inky_bird_frame.config import load_config
from inky_bird_frame.web import create_app
import inky_bird_frame.community as community
# Only this synthetic QA server replaces the remote community query.
community.get_json=lambda *args,**kwargs: {'total_results':1,'results':[{'count':4,'taxon':{'id':${cardinal.taxon_id},'rank':'species','preferred_common_name':'Northern Cardinal','name':'Cardinalis cardinalis'}}]}
config=load_config(Path(${JSON.stringify(configPath)}),load_secrets=False)
uvicorn.run(create_app(config.controller,app_config=config),host='127.0.0.1',port=18796,access_log=False)
`,
  ],
  {
    cwd: root,
    stdio: ['ignore', log, log],
    env: { PATH: process.env.PATH, PYTHONPATH: root + '/src' },
  },
);
let browser,
  page,
  phase = 'start';
(async () => {
  for (let n = 0; n < 50; n++) {
    try {
      if ((await fetch(origin + '/v1/access')).ok) break;
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  browser = await playwright[engine].launch({
    headless: true,
    ...(process.env.INKY_QA_BROWSER_EXECUTABLE
      ? { executablePath: process.env.INKY_QA_BROWSER_EXECUTABLE }
      : {}),
    ...(engine === 'chromium'
      ? { args: ['--disable-background-networking', '--use-mock-keychain'] }
      : {}),
  });
  const context = await browser.newContext({
    viewport: { width: 390, height: 844 },
    isMobile: engine !== 'firefox',
    hasTouch: engine !== 'firefox',
    reducedMotion: 'reduce',
  });
  await context.route('**/*', (route) =>
    new URL(route.request().url()).origin === origin ? route.continue() : route.abort(),
  );
  page = await context.newPage();
  await page.clock.install();
  const errors = [];
  page.on('pageerror', (error) => errors.push({ phase, message: error.message }));
  async function heading(name) {
    await page.getByRole('heading', { name, exact: true }).waitFor();
  }
  async function step(name) {
    for (let n = 0; n < 30; n++) {
      if (await page.getByRole('heading', { name, exact: true }).isVisible()) return;
      await page.clock.runFor(1100);
      await page.waitForTimeout(20);
    }
    await heading(name);
  }
  const state = () =>
    JSON.parse(fs.readFileSync(path.join(fixture, 'var/controller/journal.json')));
  phase = 'reserved-setup-rendering';
  const reservedStatus = async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    assert.equal(payload.data.setup_required, true);
    payload.data.setup_code = { state: 'ready', expires_in_seconds: 60 };
    await route.fulfill({ response, json: payload });
  };
  await context.route(origin + '/v1/access', reservedStatus);
  await page.goto(origin + '/app');
  await heading('Finish setup on the controller');
  assert.equal(await page.locator('input').count(), 0);
  await context.unroute(origin + '/v1/access', reservedStatus);
  phase = 'registration';
  await page.reload();
  await heading('Make Inky yours');
  await page.getByLabel('Username', { exact: true }).fill(username);
  await page.getByLabel('Choose your Inky password', { exact: true }).fill(password);
  await page.getByLabel('Confirm your password', { exact: true }).fill(password);
  await page.getByRole('button', { name: 'Create account', exact: true }).click();
  await heading('Your field journal');
  await page
    .getByText('No sources selected for refresh. Artwork and saved collections still work.', {
      exact: true,
    })
    .waitFor();
  assert.equal(await page.getByText(/The last saved selection may be out of date/).count(), 0);
  phase = 'birdbuddy-bootstrap-ux';
  await page.goto(origin + '/app?page=settings&source=birdbuddy');
  await heading('Bird Buddy');
  assert(await page.getByLabel('Dedicated guest email', { exact: true }).isDisabled());
  const permission = page.getByRole('checkbox', {
    name: 'I confirm that Bird Buddy has authorized automated API access for this account.',
    exact: true,
  });
  await permission.check();
  await page
    .getByLabel('Dedicated guest email', { exact: true })
    .fill('synthetic-guest@example.test');
  await page
    .getByLabel('Guest password (used only for this sign-in)', { exact: true })
    .fill('SYNTHETIC_GUEST_NOT_SENT');
  await permission.uncheck();
  assert.equal(await page.getByLabel('Dedicated guest email', { exact: true }).inputValue(), '');
  assert.equal(
    await page
      .getByLabel('Guest password (used only for this sign-in)', { exact: true })
      .inputValue(),
    '',
  );
  phase = 'favorites';
  await page.goto(origin + '/app?page=bird&bird=' + cardinal.taxon_id);
  await heading('Northern Cardinal');
  await Promise.all([
    page.waitForEvent('load'),
    page.getByRole('button', { name: 'Add to Favorites', exact: true }).click(),
  ]);
  await heading('Northern Cardinal');
  assert.equal(state().collections[0].kind, 'favorites');
  phase = 'upload';
  await page.goto(origin + '/app?page=imports&kind=ebird-archive');
  await heading('Your eBird history');
  await page
    .getByLabel('History file', { exact: true })
    .setInputFiles(path.join(fixture, 'archive.csv'));
  await page.getByRole('button', { name: 'Upload for checking', exact: true }).click();
  await step('2. Check the file');
  assert(!fs.existsSync(path.join(fixture, 'var/controller/ebird-archive-observations.json')));
  phase = 'preview';
  await page.getByRole('button', { name: 'Check this file', exact: true }).click();
  await step('3. Review history changes');
  await page.reload();
  await heading('Your eBird history');
  await heading('3. Review history changes');
  assert(!fs.existsSync(path.join(fixture, 'var/controller/ebird-archive-observations.json')));
  phase = 'history-confirmation';
  await page.getByRole('button', { name: 'Confirm history update', exact: true }).click();
  await step('4. Choose your checklists');
  assert.equal(state().collections.length, 1);
  await page.getByLabel('Trip start date', { exact: true }).fill('2001-03-01');
  await page.getByLabel('Trip end date', { exact: true }).fill('2001-03-02');
  await page.getByLabel('Trip name', { exact: true }).fill('A synthetic spring trip');
  await page.getByRole('button', { name: 'Find checklists', exact: true }).click();
  await page.getByRole('checkbox', { name: /2001-03-01/ }).check();
  assert.equal(await page.getByRole('checkbox').count(), 2);
  phase = 'trip-preview';
  await page.getByRole('button', { name: 'Review this trip', exact: true }).click();
  await step('5. Save your trip');
  await page.reload();
  await heading('Your eBird history');
  await heading('5. Save your trip');
  for (const width of [320, 390, 1280]) {
    await page.setViewportSize({ width, height: 844 });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: fixture + '/trip-review-phone.png', fullPage: true });
  phase = 'stale-trip-and-recovery';
  const session = (await (await context.request.get(origin + '/v1/owner/session')).json()).data;
  const journal = (await (await context.request.get(origin + '/v1/owner/journal')).json()).data;
  const edited = await context.request.post(origin + '/v1/owner/journal/collections/favorite', {
    headers: { Origin: origin, 'X-Inky-CSRF': session.csrf },
    data: { revision: journal.revision, taxon_id: cardinal.taxon_id, favorite: false },
  });
  assert.equal(edited.status(), 200);
  await page.getByRole('button', { name: 'Save this trip', exact: true }).click();
  await step('This step did not finish');
  await page.getByRole('button', { name: 'Check saved state', exact: true }).click();
  await step('5. Save your trip');
  await page.getByRole('button', { name: 'Edit trip selection', exact: true }).click();
  await step('4. Choose your checklists');
  assert.equal(
    await page.getByLabel('Trip name', { exact: true }).inputValue(),
    'A synthetic spring trip',
  );
  await page.getByRole('button', { name: 'Find checklists', exact: true }).click();
  await page.getByRole('button', { name: 'Review this trip', exact: true }).waitFor();
  assert(await page.getByRole('checkbox', { name: /2001-03-01/ }).isChecked());
  assert(!(await page.getByRole('checkbox', { name: /2001-03-02/ }).isChecked()));
  await page.getByRole('button', { name: 'Review this trip', exact: true }).click();
  await step('5. Save your trip');
  phase = 'trip-save';
  await page.getByRole('button', { name: 'Save this trip', exact: true }).click();
  await step('A synthetic spring trip');
  const trip = state().collections.find((group) => group.kind === 'personal-trip');
  assert.equal(trip.members.length, 1);
  assert.equal(trip.members[0].taxon_id, cardinal.taxon_id);
  assert(!fs.existsSync(path.join(fixture, 'var/controller/owner-import/upload.bin')));
  phase = 'portable-export';
  page.once('dialog', (dialog) => dialog.accept());
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Export collection', exact: true }).click();
  const download = await downloadPromise;
  const exported = fs.readFileSync(await download.path(), 'utf8');
  assert(!exported.includes('checklist_hash'));
  assert(!exported.includes('Fixture garden'));
  const portablePath = path.join(fixture, 'portable.json');
  fs.writeFileSync(portablePath, exported, { mode: 0o600 });
  phase = 'portable-import-response-loss';
  await page.goto(origin + '/app?page=import-collection');
  await heading('Import a collection');
  await page.getByLabel('Inky collection file', { exact: true }).setInputFiles(portablePath);
  await page.getByRole('button', { name: 'Import this selection', exact: true }).waitFor();
  let interrupted = false;
  await context.route('**/v1/owner/journal/collections/import', async (route) => {
    if (!interrupted) {
      interrupted = true;
      await route.fetch();
      await route.abort();
    } else await route.continue();
  });
  await page.getByRole('button', { name: 'Import this selection', exact: true }).click();
  await page.getByText(/You can retry this same save safely/).waitFor();
  const after = state().collections.length;
  await page.getByRole('button', { name: 'Import this selection', exact: true }).click();
  await step('A synthetic spring trip');
  assert.equal(state().collections.length, after);
  assert.equal(
    state().collections.filter((group) => group.kind === 'imported-selection').length,
    1,
  );
  const imported = state().collections.find((group) => group.kind === 'imported-selection');
  await page.waitForURL((url) => url.searchParams.get('view') === imported.collection_id);
  phase = 'retained-history';
  await page.goto(origin + '/app?page=imports&kind=ebird-archive');
  await heading('Your eBird history');
  await page
    .getByRole('button', { name: 'Use retained history without an upload', exact: true })
    .click();
  await step('4. Choose your checklists');
  await page.getByLabel('Trip name', { exact: true }).fill('Missing Robin fixture');
  await page.getByRole('button', { name: 'Find checklists', exact: true }).click();
  await page.getByRole('checkbox', { name: /2001-03-02/ }).check();
  await page.getByRole('button', { name: 'Review this trip', exact: true }).click();
  await step('5. Save your trip');
  await page.getByRole('button', { name: 'Save this trip', exact: true }).click();
  await step('Missing Robin fixture');
  phase = 'explicit-missing-plate-queue';
  await page.getByRole('button', { name: 'Review missing-plate requests', exact: true }).click();
  await heading('Queue missing plates?');
  assert(!fs.existsSync(path.join(fixture, 'var/controller/generation-queue.json')));
  await page.getByRole('button', { name: 'Confirm these plate requests', exact: true }).click();
  await heading('Operation outcome');
  for (let n = 0; n < 20; n++) {
    if (await page.getByText('succeeded', { exact: true }).isVisible()) break;
    await page.clock.runFor(1100);
    await page.waitForTimeout(20);
  }
  await page.getByText('succeeded', { exact: true }).waitFor();
  const queue = JSON.parse(
    fs.readFileSync(path.join(fixture, 'var/controller/generation-queue.json')),
  ).species;
  assert.equal(queue.length, 1);
  assert.equal(queue[0].taxon_id, robin.taxon_id);
  assert.deepEqual(queue[0].sources, ['owner-selection']);
  phase = 'candidate-inspection-and-approval';
  await page.goto(origin + '/app?page=work');
  await heading('Plate work');
  await page
    .getByRole('link', { name: 'Inspect Synthetic QA candidate 9999991', exact: true })
    .click();
  await heading('Review candidate');
  await page.locator('.detail-art').evaluate((image) => image.decode());
  await page
    .getByRole('checkbox', {
      name: 'I inspected the full plate, its text, anatomy and cited review. Make this artwork approved locally and eligible for existing publication.',
      exact: true,
    })
    .check();
  await page.getByRole('button', { name: 'Confirm local approval', exact: true }).click();
  await heading('Operation outcome');
  for (let n = 0; n < 20; n++) {
    if (await page.getByText('succeeded', { exact: true }).isVisible()) break;
    await page.clock.runFor(1100);
    await page.waitForTimeout(20);
  }
  await page.getByText('succeeded', { exact: true }).waitFor();
  assert(
    fs.existsSync(
      path.join(fixture, 'catalog/species/9999991-synthetic-qa-candidate-9999991/manifest.json'),
    ),
  );
  phase = 'candidate-rejection';
  await page.goto(origin + '/app?page=candidate&bird=9999992');
  await heading('Review candidate');
  await page
    .getByLabel('Why reject this candidate?', { exact: true })
    .fill('Synthetic QA rejection, retain the fixture privately.');
  await page.getByRole('button', { name: 'Confirm rejection', exact: true }).click();
  await heading('Operation outcome');
  for (let n = 0; n < 20; n++) {
    if (await page.getByText('succeeded', { exact: true }).isVisible()) break;
    await page.clock.runFor(1100);
    await page.waitForTimeout(20);
  }
  await page.getByText('succeeded', { exact: true }).waitFor();
  assert(
    fs.existsSync(
      path.join(
        fixture,
        'var/controller/rejected/9999992-synthetic-qa-candidate-9999992/manifest.json',
      ),
    ),
  );
  phase = 'pair-readonly-screen';
  await page.goto(origin + '/app?page=displays');
  await heading('Displays');
  const screenContext = await browser.newContext({
    viewport: { width: 390, height: 844 },
    isMobile: engine !== 'firefox',
    hasTouch: engine !== 'firefox',
    reducedMotion: 'reduce',
  });
  await screenContext.route('**/*', (route) =>
    new URL(route.request().url()).origin === origin ? route.continue() : route.abort(),
  );
  const screen = await screenContext.newPage();
  await screen.clock.install();
  screen.on('pageerror', (error) => errors.push({ phase, message: error.message }));
  await screen.goto(origin + '/screen');
  await screen.getByRole('button', { name: 'Pair this screen', exact: true }).click();
  await screen.getByRole('heading', { name: 'Pair this screen', exact: true }).waitFor();
  const pairCode = await screen.locator('.pairing-code').innerText();
  await page.reload();
  await page.getByRole('heading', { name: 'Pairing request · ' + pairCode, exact: true }).waitFor();
  await page.getByLabel('Screen name', { exact: true }).fill('Synthetic tablet');
  await page.getByLabel('Permitted view', { exact: true }).first().selectOption(trip.collection_id);
  await page
    .getByLabel('I matched this code on the screen I want to pair', { exact: true })
    .check();
  await Promise.all([
    page.waitForEvent('load'),
    page.getByRole('button', { name: 'Pair this screen', exact: true }).click(),
  ]);
  await screen.clock.runFor(15100);
  await screen.locator('.frame-art').waitFor();
  await screen.locator('.frame-art').evaluate((image) => image.decode());
  assert.equal((await screenContext.request.get(origin + '/v1/owner/journal')).status(), 401);
  const screenView = (await (await screenContext.request.get(origin + '/v1/screen/view')).json())
    .data;
  assert.equal(screenView.species.length, 1);
  assert.equal(screenView.species[0].taxon_id, cardinal.taxon_id);
  phase = 'authoritative-screen-scope-removal';
  const replacement = publicCatalog.find(
    (bird) => bird.taxon_id !== cardinal.taxon_id && bird.taxon_id !== robin.taxon_id,
  );
  fs.cpSync(
    path.join(root, 'catalog', path.dirname(replacement.portrait_path)),
    path.join(fixture, 'catalog', path.dirname(replacement.portrait_path)),
    { recursive: true },
  );
  const catalogFile = path.join(fixture, 'catalog/index.json'),
    expanded = JSON.parse(fs.readFileSync(catalogFile));
  expanded.species.push(replacement);
  fs.writeFileSync(catalogFile, JSON.stringify(expanded));
  const ownerSession = (await (await context.request.get(origin + '/v1/owner/session')).json())
    .data;
  let currentJournal = (await (await context.request.get(origin + '/v1/owner/journal')).json())
    .data;
  await screenContext.route('**/v1/screen/plates/' + replacement.taxon_id + '?*', (route) =>
    route.fulfill({ status: 503, contentType: 'application/json', body: '{"ok":false}' }),
  );
  assert.equal(
    (
      await context.request.post(origin + '/v1/owner/journal/collections/save', {
        headers: { Origin: origin, 'X-Inky-CSRF': ownerSession.csrf },
        data: {
          revision: currentJournal.revision,
          collection_id: trip.collection_id,
          label: trip.label,
          taxon_ids: [replacement.taxon_id],
        },
      })
    ).status(),
    200,
  );
  await screen.clock.runFor(15100);
  await screen.getByText(/temporarily unavailable/).waitFor();
  assert.equal(await screen.locator('img[src]').count(), 0);
  currentJournal = (await (await context.request.get(origin + '/v1/owner/journal')).json()).data;
  assert.equal(
    (
      await context.request.post(origin + '/v1/owner/journal/collections/remove', {
        headers: { Origin: origin, 'X-Inky-CSRF': ownerSession.csrf },
        data: { revision: currentJournal.revision, collection_id: trip.collection_id },
      })
    ).status(),
    200,
  );
  await screen.clock.runFor(15100);
  await screen
    .getByText('No approved plates in the permitted view. Inky will not switch views.', {
      exact: true,
    })
    .waitFor();
  assert.equal(await screen.locator('img[src]').count(), 0);
  await page.getByRole('button', { name: 'Revoke screen', exact: true }).click();
  await screen.clock.runFor(15100);
  await screen.getByRole('heading', { name: 'Screen access ended', exact: true }).waitFor();
  assert.equal(await screen.locator('img').count(), 0);
  phase = 'scoped-integrations';
  await page.goto(origin + '/app?page=integrations');
  await heading('Integrations');
  await page
    .getByLabel('Permitted view', { exact: true })
    .first()
    .selectOption(imported.collection_id);
  await page
    .getByLabel(
      'I approve exposing this selected species membership on my trusted network or VPN',
      { exact: true },
    )
    .check();
  await Promise.all([
    page.waitForEvent('load'),
    page.getByRole('button', { name: 'Enable this art view', exact: true }).click(),
  ]);
  const artResponse = await context.request.get(origin + '/v1/dashboard/art');
  assert.equal(artResponse.status(), 200);
  const artText = await artResponse.text();
  assert(!artText.includes(trip.label));
  assert(!artText.includes('2001-03-01'));
  await page.getByRole('button', { name: 'Create status credential', exact: true }).click();
  await page.getByRole('heading', { name: 'Copy this credential now', exact: true }).waitFor();
  const serviceSecret = await page.locator('.notice pre').first().innerText();
  const aggregate = await context.request.get(origin + '/v1/integrations/status', {
    headers: { Authorization: 'Bearer ' + serviceSecret },
  });
  assert.equal(aggregate.status(), 200);
  assert(!JSON.stringify(await aggregate.json()).includes(trip.label));
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await Promise.all([
    page.waitForEvent('load'),
    page.getByRole('button', { name: 'I saved it · clear this copy', exact: true }).click(),
  ]);
  await heading('Integrations');
  assert.equal(await page.getByText(serviceSecret, { exact: true }).count(), 0);
  await Promise.all([
    page.waitForEvent('load'),
    page.getByRole('button', { name: 'Revoke Dashboard status', exact: true }).click(),
  ]);
  await heading('Integrations');
  assert.equal(
    (
      await context.request.get(origin + '/v1/integrations/status', {
        headers: { Authorization: 'Bearer ' + serviceSecret },
      })
    ).status(),
    401,
  );
  await screenContext.close();
  for (const filename of ['collection.json', 'display-last-success.json'])
    assert(!fs.existsSync(path.join(fixture, 'var/controller', filename)));
  phase = 'community-wizard';
  await page.goto(origin + '/app?page=community');
  await heading('Birds reported near a place');
  await page.getByRole('button', { name: 'New community search', exact: true }).click();
  for (const [label, value] of [
    ['Collection name', 'Synthetic community selection'],
    ['Start date', '2001-03-01'],
    ['End date', '2001-03-02'],
    ['Latitude', '0.125'],
    ['Longitude', '0.25'],
    ['Radius in km', '10'],
  ])
    await page.getByLabel(label, { exact: true }).fill(value);
  await page.getByRole('button', { name: 'Check community reports', exact: true }).click();
  await heading('Operation outcome');
  for (let n = 0; n < 20; n++) {
    if (await page.getByText('succeeded', { exact: true }).isVisible()) break;
    await page.clock.runFor(1100);
    await page.waitForTimeout(20);
  }
  await page.getByRole('link', { name: 'Review community selection', exact: true }).click();
  await heading('Synthetic community selection');
  assert(!state().collections.some((group) => group.kind === 'community-history'));
  await page.getByRole('button', { name: 'Save community collection', exact: true }).click();
  await heading('Operation outcome');
  for (let n = 0; n < 20; n++) {
    if (await page.getByText('succeeded', { exact: true }).isVisible()) break;
    await page.clock.runFor(1100);
    await page.waitForTimeout(20);
  }
  const communityGroup = state().collections.find((group) => group.kind === 'community-history');
  assert.equal(communityGroup.members[0].origin, 'community-report');
  assert.deepEqual(communityGroup.members[0].evidence, []);
  phase = 'manual-community-member-label';
  const communitySession = (
    await (await context.request.get(origin + '/v1/owner/session')).json()
  ).data;
  const manualAddition = await context.request.post(
    origin + '/v1/owner/journal/collections/save',
    {
      headers: { Origin: origin, 'X-Inky-CSRF': communitySession.csrf },
      data: {
        revision: state().revision,
        collection_id: communityGroup.collection_id,
        label: communityGroup.label,
        taxon_ids: [cardinal.taxon_id, replacement.taxon_id],
      },
    },
  );
  assert.equal(manualAddition.status(), 200);
  await page.goto(origin + '/app?page=collection&view=' + communityGroup.collection_id);
  await heading('Synthetic community selection');
  assert.equal(
    await page.getByText('Community report · not your personal sighting', { exact: true }).count(),
    1,
  );
  assert.equal(
    await page.getByText('Manual selection · no community report linked', { exact: true }).count(),
    1,
  );
  phase = 'health-and-guides';
  await page.goto(origin + '/app?page=health');
  await heading('Health and help');
  await page.getByRole('heading', { name: 'Approved catalog index · ok', exact: true }).waitFor();
  for (const [area, title] of [
    ['backups', 'Backups and upgrades'],
    ['advanced', 'Advanced tools'],
  ]) {
    await page.goto(origin + '/app?page=' + area);
    await heading(title);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  }
  phase = 'saved-birds-explicit-confirmation';
  await page.goto(origin + '/app?page=bird&bird=' + cardinal.taxon_id);
  await heading('Northern Cardinal');
  await page.getByRole('button', { name: 'Review adding to Saved birds', exact: true }).click();
  await heading('Change Saved birds?');
  assert(!fs.existsSync(path.join(fixture, 'var/controller/collection.json')));
  await page.getByRole('button', { name: 'Confirm Saved birds change', exact: true }).click();
  await heading('Operation outcome');
  for (let n = 0; n < 20; n++) {
    if (await page.getByText('succeeded', { exact: true }).isVisible()) break;
    await page.clock.runFor(1100);
    await page.waitForTimeout(20);
  }
  assert.equal(
    JSON.parse(fs.readFileSync(path.join(fixture, 'var/controller/collection.json'))).taxa[0]
      .taxon_id,
    cardinal.taxon_id,
  );
  phase = 'owner-frame';
  await page.goto(origin + '/app?page=frame&view=' + imported.collection_id);
  await page.locator('.frame-art').waitFor();
  await page.locator('.frame-art').evaluate((image) => image.decode());
  await page.getByRole('button', { name: 'Zoom', exact: true }).click();
  await page.getByRole('button', { name: 'Fit whole plate', exact: true }).click();
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  if (process.env.INKY_QA_UPLOAD_FILE) {
    phase = 'large-mobile-upload';
    await page.goto(origin + '/app?page=imports&kind=ebird-archive');
    await heading('Your eBird history');
    await page
      .getByLabel('History file', { exact: true })
      .setInputFiles(process.env.INKY_QA_UPLOAD_FILE);
    await page.getByRole('button', { name: 'Upload for checking', exact: true }).click();
    await step('2. Check the file');
    assert.equal(
      fs.statSync(path.join(fixture, 'var/controller/owner-import/upload.bin')).size,
      fs.statSync(process.env.INKY_QA_UPLOAD_FILE).size,
    );
    await page.getByRole('button', { name: 'Cancel preview', exact: true }).click();
    await heading('1. Choose the complete source file');
    assert(!fs.existsSync(path.join(fixture, 'var/controller/owner-import/upload.bin')));
  }
  if (process.env.INKY_QA_AXE) {
    const { default: AxeBuilder } = require(process.env.INKY_QA_AXE),
      audits = [];
    for (const [area, title] of [
      ['home', 'Your field journal'],
      ['birds', 'Your birds'],
      ['collections', 'Collections'],
      ['manage', 'Manage'],
      ['health', 'Health and help'],
      ['integrations', 'Integrations'],
      ['displays', 'Displays'],
      ['settings', 'Settings and backups'],
    ]) {
      phase = 'accessibility-' + area;
      await page.goto(origin + '/app?page=' + area);
      await heading(title);
      const result = await new AxeBuilder({ page }).analyze();
      audits.push({
        page: area,
        violations: result.violations.map(({ id, impact, nodes }) => ({
          id,
          impact,
          targets: nodes.map((node) => node.target),
        })),
      });
    }
    fs.writeFileSync(fixture + '/accessibility-result.json', JSON.stringify(audits, null, 2), {
      mode: 0o600,
    });
    assert(
      audits.every((result) => result.violations.length === 0),
      'automated accessibility violations need review',
    );
  }
  assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
  assert.deepEqual(errors, []);
  fs.writeFileSync(
    fixture + '/history-browser-result.json',
    JSON.stringify(
      {
        passed: true,
        synthetic: true,
        communityQuery: 'mocked',
        reservedSetupRendering: 'synthetic access status; rejection tested through real backend',
        engine,
        sourceCommit: execFileSync('git', ['rev-parse', 'HEAD'], {
          cwd: root,
          encoding: 'utf8',
        }).trim(),
        sourceDirty:
          execFileSync('git', ['status', '--porcelain'], { cwd: root, encoding: 'utf8' }).trim()
            .length > 0,
        largeUpload: !!process.env.INKY_QA_UPLOAD_FILE,
        accessibility: !!process.env.INKY_QA_AXE,
        checks: [
          'owner setup',
          'Favorites separate from sightings',
          'complete archive upload',
          'separate history confirmation',
          'explicit checklist selection',
          'stale edit and response-loss recovery',
          'portable collection export/import',
          'retained history',
          'explicit missing-plate requests',
          'strict candidate approval/rejection',
          'paired-screen scope withdrawal and revocation',
          'scoped dashboard art',
          'separate status credential and revocation',
          'community evidence not personal',
          'Health and operator guidance',
          'explicit Saved birds preview',
          'Frame viewing tools',
          'no browser storage credentials',
          'no model process or physical panel write',
          'no script errors',
        ],
      },
      null,
      2,
    ),
    { mode: 0o600 },
  );
  console.log('Journal browser acceptance passed (' + engine + '). Synthetic evidence: ' + fixture);
})()
  .catch(async (error) => {
    if (page)
      await page
        .screenshot({ path: fixture + '/history-browser-failure.png', fullPage: true })
        .catch(() => {});
    fs.writeFileSync(
      fixture + '/failure.json',
      JSON.stringify(
        {
          engine,
          phase,
          type: error.name,
          actual: Array.isArray(error.actual) ? error.actual : undefined,
        },
        null,
        2,
      ),
      { mode: 0o600 },
    );
    console.error('History browser failed at ' + phase + ': ' + error.name);
    console.error(
      error.stack
        ?.split('\n')
        .filter((line) => line.trimStart().startsWith('at '))
        .join('\n'),
    );
    console.error('Synthetic failure evidence: ' + fixture);
    process.exitCode = 1;
  })
  .finally(async () => {
    if (browser) await browser.close();
    server.kill('SIGTERM');
    fs.closeSync(log);
  });
