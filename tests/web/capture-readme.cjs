// Capture real app pages using public catalog art and a disposable local owner.
const assert = require('node:assert/strict');
const { randomBytes } = require('node:crypto');
const { spawn } = require('node:child_process');
const { mkdirSync, mkdtempSync, writeFileSync } = require('node:fs');
const { tmpdir } = require('node:os');
const path = require('node:path');
const { createInterface } = require('node:readline');
const { chromium } = require(process.env.INKY_QA_PLAYWRIGHT || 'playwright');

const root = path.resolve(__dirname, '../..');
const python = process.env.INKY_QA_PYTHON || path.join(root, '.venv/bin/python');
const options = process.argv.slice(2);
assert(options.filter((value) => !value.startsWith('--')).length <= 1);
assert(options.filter((value) => value.startsWith('--'))
  .every((value) => ['--walkthrough', '--walkthrough-v2', '--export-sample'].includes(value)));
const output = path.resolve(options.find((value) => !value.startsWith('--')) ||
  mkdtempSync(path.join(tmpdir(), 'inky-images-')));
const cinematic = process.argv.includes('--walkthrough-v2');
const walkthrough = cinematic || process.argv.includes('--walkthrough');
mkdirSync(output, { recursive: true });
const fixture = mkdtempSync(path.join(tmpdir(), 'inky-readme-fixture-'));
const server = spawn(python, ['-I', '-c', `
import json, shutil, socket, sys, tomllib
from pathlib import Path
import uvicorn
from inky_bird_frame.config import load_config
from inky_bird_frame.settings import _toml, initialize_config
from inky_bird_frame.web import create_app

fixture, repo = map(Path, sys.argv[1:])
config_path = fixture / 'config.toml'
initialize_config(config_path)
catalog = fixture / 'catalog'
rows = json.loads((repo / 'catalog/index.json').read_text())['species']
birds = [row for row in rows if row['taxon_id'] in {9083, 12727}]
assert len(birds) == 2
for bird in birds:
    folder = Path(bird['portrait_path']).parent
    shutil.copytree(repo / 'catalog' / folder, catalog / folder)
(catalog / 'index.json').write_text(json.dumps({
    'schema_version': 1, 'generated_at': birds[0]['approved_at'], 'species': birds,
}))
with socket.socket() as listener:
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    raw = tomllib.loads(config_path.read_text())
    raw['controller'].update(catalog_dir=str(catalog), port=port, bind_host='127.0.0.1')
    config_path.write_bytes(_toml(raw))
    config = load_config(config_path, load_secrets=False)
    print(port, flush=True)
    uvicorn.Server(uvicorn.Config(
        create_app(config.controller, app_config=config), access_log=False,
    )).run(sockets=[listener])
`, fixture, root], {
  cwd: fixture,
  env: { HOME: fixture, PATH: process.env.PATH },
  stdio: ['ignore', 'pipe', 'inherit'],
});

async function capture() {
  const lines = createInterface({ input: server.stdout });
  const { value: port } = await lines[Symbol.asyncIterator]().next();
  assert.match(port || '', /^\d+$/);
  const origin = `http://127.0.0.1:${port}`;
  const browser = await chromium.launch({
    headless: true,
    args: ['--disable-background-networking', '--use-mock-keychain'],
  });
  try {
    const context = await browser.newContext({
      viewport: { width: 430, height: 1200 }, isMobile: true, hasTouch: true,
      reducedMotion: 'reduce', colorScheme: 'light', serviceWorkers: 'block',
    });
    await context.route('**/*', (route) =>
      new URL(route.request().url()).origin === origin ? route.continue() : route.abort(),
    );
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    const password = randomBytes(32).toString('base64url');
    await page.goto(origin + '/app');
    await page.getByLabel('Username', { exact: true }).fill('demo-owner');
    await page.getByLabel('Choose your Inky password', { exact: true }).fill(password);
    await page.getByLabel('Confirm your password', { exact: true }).fill(password);
    await page.getByRole('button', { name: 'Create account', exact: true }).click();
    await page.getByRole('heading', { name: 'Your field journal', exact: true }).waitFor();

    await page.goto(origin + '/app?page=edit-collection');
    await page.getByLabel('Collection name', { exact: true }).fill('Garden birds');
    await page.getByRole('checkbox', { name: /Northern Cardinal/ }).check();
    await page.getByRole('checkbox', { name: /American Robin/ }).check();
    await page.getByRole('button', { name: 'Save collection', exact: true }).click();
    await page.getByRole('link', { name: 'Garden birds', exact: true }).click();
    await page.getByRole('heading', { name: 'Garden birds', exact: true }).waitFor();

    async function image(name) {
      await page.evaluate(async () => {
        await document.fonts.ready;
        for (const image of document.querySelectorAll('main img')) await image.decode();
      });
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.screenshot({ path: path.join(output, name), scale: 'css', animations: 'disabled' });
    }
    await page.evaluate(() => scrollTo(0, 0));
    await image('journal-collection.png');
    await page.goto(origin + '/app?page=bird&bird=9083');
    await page.getByRole('button', { name: 'Review adding to Saved birds', exact: true }).click();
    await page.getByRole('button', { name: 'Confirm Saved birds change', exact: true }).click();
    await page.getByRole('heading', { name: 'Operation outcome', exact: true }).waitFor();
    await page.getByText('succeeded', { exact: true }).waitFor();
    await page.goto(origin + '/app');
    await page.getByRole('heading', { name: 'Your field journal', exact: true }).waitFor();
    await page.getByRole('link', { name: 'Play', exact: true }).waitFor();
    await image('journal-home.png');
    if (process.argv.includes('--export-sample')) {
      const sample = await page.evaluate(async () => {
        const response = await fetch('/v1/owner/journal', { credentials: 'same-origin' });
        if (!response.ok) throw new Error('Synthetic journal export failed');
        return (await response.json()).data;
      });
      writeFileSync(path.join(output, 'sample-journal.json'), JSON.stringify(sample, null, 2) + '\n');
    }
    if (walkthrough) {
      // Start a fresh recording only after account creation and fixture setup.
      // Keep synthetic session cookies in memory, never in the output directory.
      const videoContext = await browser.newContext({
        // Chromium's screencast is CSS-resolution even on a Retina context.
        // Record the actual desktop layout, not a padded or CSS-zoomed view.
        viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1,
        recordVideo: { dir: output, size: { width: 1280, height: 800 } },
        storageState: await context.storageState(),
        reducedMotion: 'reduce', colorScheme: 'light', serviceWorkers: 'block',
      });
      const blocked = [];
      await videoContext.route('**/*', (route) => {
        if (new URL(route.request().url()).origin === origin) return route.continue();
        blocked.push(route.request().url());
        return route.abort();
      });
      const film = await videoContext.newPage();
      const video = film.video();
      const started = performance.now();
      film.on('pageerror', (error) => errors.push(error.message));
      await film.goto(origin + '/app');
      await film.getByRole('heading', { name: 'Your field journal', exact: true }).waitFor();
      await film.locator('main img').first().evaluate((image) => image.decode());
      const trimStart = (performance.now() - started) / 1000;
      const sceneStart = performance.now();
      const events = [];
      async function at(seconds, name, action) {
        const remaining = seconds * 1000 - (performance.now() - sceneStart);
        if (remaining > 0) await new Promise((resolve) => setTimeout(resolve, remaining));
        events.push({ seconds: (performance.now() - sceneStart) / 1000, action: name });
        if (action) await action();
      }
      await film.screenshot({ path: path.join(output, 'walkthrough-home.png') });
      await at(3, 'Open Collections', async () => {
        await film.getByRole('navigation', { name: 'Journal', exact: true })
          .getByRole('link', { name: 'Collections', exact: true }).click();
        await film.getByText('1 plate ready · 0 not ready', { exact: true }).first().waitFor();
        await film.getByText('2 plates ready · 0 not ready', { exact: true }).first().waitFor();
        assert.equal(await film.getByText(/1 plates ready/).count(), 0);
      });
      await at(5, 'Open Garden birds', () =>
        film.getByRole('link', { name: 'Garden birds', exact: true }).click());
      await film.getByRole('heading', { name: 'Garden birds', exact: true }).waitFor();
      await at(8, 'Open Frame mode', () =>
        film.getByRole('link', { name: 'Play', exact: true }).click());
      await film.getByAltText('Approved plate: Northern Cardinal', { exact: true }).waitFor();
      await at(9, 'Start Play', () =>
        film.getByRole('button', { name: 'Play', exact: true }).click());
      await at(12, 'Manual Next', () =>
        film.getByRole('button', { name: 'Next', exact: true }).click());
      await film.getByAltText('Approved plate: American Robin', { exact: true }).waitFor();
      await film.screenshot({ path: path.join(output, 'walkthrough-frame.png') });
      await at(16, 'End screen recording');
      await videoContext.close();
      writeFileSync(path.join(output, 'walkthrough-capture.json'), JSON.stringify({
        source: 'Checked-out real app with two approved public plates and a sample collection',
        viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1,
        browserZoom: 1,
        cinematic,
        video: path.basename(await video.path()), trimStart, duration: 16,
        events, blockedExternalRequests: blocked, pageErrors: errors,
      }, null, 2) + '\n');
      assert.deepEqual(blocked, []);
    }
    assert.deepEqual(errors, []);
    console.log(`Screenshots: ${output}`);
  } finally {
    await browser.close();
  }
}

capture().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
}).finally(() => server.kill());
