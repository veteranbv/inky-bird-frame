// Render an inspected sample-app capture. Generated media stays outside Git.
const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const { readFileSync, writeFileSync, statSync } = require('node:fs');
const { createHash } = require('node:crypto');
const path = require('node:path');
const { verifyOpeningFrame } = require('./walkthrough-preview.cjs');
const { chromium } = require(process.env.INKY_QA_PLAYWRIGHT || 'playwright');

const output = path.resolve(process.argv[2]);
const root = path.resolve(__dirname, '../..');
const capture = JSON.parse(readFileSync(path.join(output, 'walkthrough-capture.json')));
assert.deepEqual(capture.pageErrors, []);
assert.deepEqual(capture.blockedExternalRequests, []);
assert.equal(path.basename(capture.video), capture.video);
const captions = [
  [0, 3, 'The real app, sample data'],
  [3, 8, 'Keep a collection of birds you want to revisit.'],
  [8, 11, 'Play approved plates on a browser screen.'],
  [11, 16, 'Click Next to change this browser view. Automatic rotation is once a minute.'],
  [16, 20, 'A separate e-paper frame can show the same plates.'],
];

function command(executable, args) {
  const result = spawnSync(executable, args, { encoding: 'utf8' });
  if (result.status !== 0) throw new Error(result.stderr || result.error?.message);
  return result.stdout;
}

async function render() {
  const browser = await chromium.launch({ headless: true,
    args: ['--disable-background-networking', '--use-mock-keychain'] });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 64 } });
    await page.route('**/*', (route) => route.abort());
    for (let index = 0; index < captions.length; index += 1) {
      await page.setContent(`<style>
        * { box-sizing:border-box } body { margin:0; width:1280px; height:64px;
        background:#1d3029; color:#fffdf5; font:600 28px/1.2 system-ui,sans-serif;
        display:grid; place-items:center; text-align:center; padding:0 32px }
      </style><body></body>`);
      await page.locator('body').evaluate((node, text) => { node.textContent = text; }, captions[index][2]);
      await page.screenshot({ path: path.join(output, `caption-${index}.png`) });
    }
  } finally { await browser.close(); }

  const args = ['-hide_banner', '-loglevel', 'error', '-n', '-ss', String(capture.trimStart),
    '-i', path.join(output, capture.video), '-loop', '1', '-i',
    path.join(root, 'docs/images/framed-installation.jpg')];
  for (let index = 0; index < captions.length; index += 1)
    args.push('-loop', '1', '-i', path.join(output, `caption-${index}.png`));
  const filters = [
    '[0:v]trim=duration=16,setpts=PTS-STARTPTS,fps=30,scale=1280:736:force_original_aspect_ratio=decrease,pad=1280:800:(ow-iw)/2:0:color=0xf4efe5,setsar=1[app]',
    '[1:v]trim=duration=4,setpts=PTS-STARTPTS,fps=30,scale=1280:736:force_original_aspect_ratio=decrease,pad=1280:800:(ow-iw)/2:0:color=0x15120d,setsar=1[wall]',
    '[app][wall]concat=n=2:v=1:a=0[base]',
  ];
  for (let index = 0; index < captions.length; index += 1) {
    const [start, end] = captions[index];
    filters.push(`[${index ? `c${index - 1}` : 'base'}][${index + 2}:v]overlay=0:736:enable='gte(t,${start})*lt(t,${end})'[c${index}]`);
  }
  args.push('-filter_complex', filters.join(';'), '-map', '[c4]', '-t', '20', '-an',
    '-c:v', 'libx264', '-crf', '23', '-preset', 'medium', '-pix_fmt', 'yuv420p',
    '-map_metadata', '-1', '-movflags', '+faststart', path.join(output, 'inky-walkthrough.mp4'));
  command(process.env.INKY_QA_FFMPEG || 'ffmpeg', args);
  const ffmpeg = process.env.INKY_QA_FFMPEG || 'ffmpeg';
  command(ffmpeg, ['-hide_banner', '-loglevel', 'error', '-n', '-i',
    path.join(output, 'inky-walkthrough.mp4'), '-filter_complex',
    '[0:v]fps=8,scale=768:480:flags=lanczos,split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=3',
    '-loop', '-1', path.join(output, 'inky-walkthrough-share.gif')]);
  command(ffmpeg, ['-hide_banner', '-loglevel', 'error', '-n', '-ss', '10', '-i',
    path.join(output, 'inky-walkthrough.mp4'), '-frames:v', '1',
    path.join(output, 'inky-walkthrough-poster.png')]);
  writeFileSync(path.join(output, 'inky-walkthrough-transcript.md'),
    '# Inky Bird walkthrough\n\nThe real app, with two public approved plates and a sample collection. '
    + 'No personal sightings or production services appear. This silent video does not autoplay.\n\n'
    + '- 0:00–0:03: Home shows the Northern Cardinal plate.\n'
    + '- 0:03–0:08: Open Collections, then the sample Garden birds collection.\n'
    + '- 0:08–0:11: Open Frame mode and start Play.\n'
    + '- 0:11–0:16: Click Next manually to show American Robin. Automatic playback changes plates once a minute.\n'
    + '- 0:16–0:20: Cut to a photograph of a separate e-paper frame showing Eastern Bluebird. '
    + 'Browser playback does not trigger that physical display.\n');
  const stamp = (seconds) => `00:${String(seconds).padStart(2, '0')}.000`;
  writeFileSync(path.join(output, 'inky-walkthrough.vtt'), 'WEBVTT\n\n' + captions.map(
    ([start, end, text]) => `${stamp(start)} --> ${stamp(end)}\n${text}\n`,
  ).join('\n'));
  console.log(`Media: ${output}`);
}

async function renderCinematic() {
  assert.equal(capture.cinematic, true, 'Record with --walkthrough-v2 first');
  assert.deepEqual(capture.viewport, { width: 1280, height: 800 });
  assert.equal(capture.deviceScaleFactor, 1);
  assert.equal(capture.browserZoom, 1);
  const css = readFileSync(path.join(root, 'src/inky_bird_frame/web/app.css'), 'utf8');
  const tokenStart = css.indexOf(':root {');
  const tokens = css.slice(tokenStart, css.indexOf('\n}', tokenStart) + 2);
  assert(tokens.includes('--paper:') && tokens.includes('--serif:'));
  const catalog = JSON.parse(readFileSync(path.join(root, 'catalog/index.json'))).species;
  const cardinal = path.join(root, 'catalog', catalog.find(b => b.taxon_id === 9083).portrait_path);
  const mark = `data:image/png;base64,${readFileSync(path.join(root, 'docs/images/inky-bird-mark.png')).toString('base64')}`;
  const plate = `data:image/png;base64,${readFileSync(cardinal).toString('base64')}`;
  const browser = await chromium.launch({ headless: true,
    args: ['--disable-background-networking', '--use-mock-keychain'] });
  const page = await browser.newPage({ viewport: { width: 1920, height: 1200 },
    colorScheme: 'light', reducedMotion: 'reduce', serviceWorkers: 'block' });
  const errors = [], blocked = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/*', route => {
    blocked.push(route.request().url());
    return route.abort();
  });
  async function still(name, body, extra = '', size = { width: 1920, height: 1200 }) {
    await page.setViewportSize(size);
    await page.setContent(`<meta charset="utf-8"><style>${tokens}
      *{box-sizing:border-box}body{margin:0;width:${size.width}px;height:${size.height}px;
      color:var(--ink);background:var(--paper);overflow:hidden}
      h1{margin:0;font:700 156px/1.1 var(--serif);letter-spacing:-.025em}
      .brand{display:flex;align-items:baseline;justify-content:center;gap:24px}
      .brand img{height:1cap;width:auto}p{margin:32px 0 0;font:38px/1.4 var(--serif)}
      .center{height:100%;display:grid;place-content:center;text-align:center}
      ${extra}</style><body>${body}</body>`);
    await page.evaluate(async () => {
      await document.fonts.ready;
      for (const image of document.images) await image.decode();
    });
    await page.screenshot({ path: path.join(output, name), omitBackground: true });
  }
  const brand = `<h1 class="brand">Inky Bird<img src="${mark}" alt=""></h1>`;
  const lines = [
    [2.5, 7, 'Reviewed field-journal plates, ready to revisit.'],
    [7, 12, 'Keep the birds you want to revisit.'],
    [12, 19, 'Press Play. Plates change once a minute; Next skips ahead.'],
    [19, 24, 'A separate e-paper frame can show the same plates.'],
  ];
  try {
    await still('v2-end.png', `<div class="center">${brand}<p>Browse the gallery · henrysowell.com/birds</p><p class="repo">github.com/veteranbv/inky-bird</p></div>`,
      '.repo{font:26px/1.5 system-ui,sans-serif;color:var(--ink-soft);margin-top:18px}');
    await still('v2-backdrop.png', '<div class="card"></div><div class="sample">Sample data</div>',
      '.card{position:absolute;left:180px;top:76px;width:1560px;height:975px;background:var(--paper-raised);border-radius:calc(var(--radius)*2);box-shadow:var(--shadow)}.sample{position:absolute;right:180px;top:20px;font:600 22px/1.4 system-ui,sans-serif;letter-spacing:.025em;color:var(--accent);padding:5px 16px;background:var(--accent-soft);border-radius:20px}');
    await still('v2-mask.png', '<div></div>',
      'body{background:#000}body>div{width:100%;height:100%;border-radius:calc(var(--radius)*2);background:#fff}',
      { width: 1560, height: 975 });
    for (let i = 0; i < lines.length; i += 1)
      await still(`v2-caption-${i}.png`, `<div>${lines[i][2]}</div>`,
        'body{background:transparent}body>div{height:100%;display:grid;place-items:center;margin:0 70px;padding:12px 32px;background:rgba(246,241,231,.94);border:1px solid var(--line);border-radius:var(--radius);font:44px/1.2 var(--serif);white-space:nowrap}',
        { width: 1920, height: 100 });
    await still('inky-walkthrough-poster.png', `<img class="art" src="${plate}" alt=""><div class="poster">${brand}<p>A field journal for the birds you see.</p></div>`,
      '.art{width:100%;height:100%;object-fit:cover;object-position:50% 8%}.poster{position:absolute;left:70px;bottom:65px;padding:32px 48px;background:rgba(246,241,231,.94);border:1px solid var(--line);border-radius:var(--radius)}.poster h1{font-size:78px;justify-content:flex-start}.poster p{font-size:28px;margin-top:12px}');
    assert.deepEqual(blocked, []);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }

  const ffmpeg = process.env.INKY_QA_FFMPEG || 'ffmpeg';
  const args = ['-hide_banner', '-loglevel', 'error', '-n', '-i', path.join(output, capture.video)];
  for (const file of [cardinal, path.join(root, 'docs/images/framed-installation.jpg'),
    ...['inky-walkthrough-poster.png', 'v2-end.png', 'v2-backdrop.png', 'v2-mask.png',
      ...lines.map((_, i) => `v2-caption-${i}.png`)].map(name => path.join(output, name))])
    args.push('-loop', '1', '-framerate', '30', '-i', file);
  const normalize = 'fps=30,format=yuv420p,settb=AVTB,setpts=PTS-STARTPTS';
  const start = Number(capture.trimStart);
  assert(Number.isFinite(start) && start >= 0);
  const filters = [
    // GitHub's unplayed preview may use frame zero: start with the full poster,
    // including artwork and title, rather than fading up from blank paper.
    `[3:v]trim=duration=2.9,${normalize}[title]`,
    `[1:v]scale=1920:1200:force_original_aspect_ratio=increase,crop=1920:1200:0:(ih-oh)*0.08,zoompan=z='1+0.035*on/146':x='(iw-iw/zoom)*0.65':y='(ih-ih/zoom)*0.35':d=1:s=1920x1200:fps=30,trim=duration=4.9,${normalize}[art]`,
    '[0:v]split=2[rawjournal][rawframe]',
    `[rawjournal]trim=start=${start}:end=${start + 8},setpts=(PTS-STARTPTS)*5.4/8,fps=30,scale=1560:975:flags=lanczos,setsar=1,format=yuv420p[jui]`,
    `[rawframe]trim=start=${start + 8}:end=${start + 16},setpts=(PTS-STARTPTS)*7/8,fps=30,scale=1560:975:flags=lanczos,setsar=1,format=yuv420p[fui]`,
    '[6:v]format=gray,split=2[jmask][fmask]',
    '[jui][jmask]alphamerge[jrounded]',
    '[fui][fmask]alphamerge[frounded]',
    '[5:v]split=2[jbg][fbg]',
    `[jbg][jrounded]overlay=180:76:shortest=1,trim=duration=5.4,${normalize}[journal]`,
    `[fbg][frounded]overlay=180:76:shortest=1,trim=duration=7,zoompan=z='1+0.015*on/209':x='(iw-iw/zoom)/2':y='(ih-ih/zoom)/2':d=1:s=1920x1200:fps=30,${normalize}[frame]`,
    '[title][art]xfade=transition=fade:duration=0.4:offset=2.5[a]',
    '[a][journal]xfade=transition=fade:duration=0.4:offset=7[b]',
    '[b][frame]xfade=transition=fade:duration=0.4:offset=12[opening]',
    `[2:v]scale=1920:1200:force_original_aspect_ratio=increase,crop=1920:1200:(iw-ow)/2:(ih-oh)*0.18,zoompan=z='1+0.025*on/161':x='(iw-iw/zoom)/2':y='(ih-ih/zoom)/2':d=1:s=1920x1200:fps=30,trim=duration=5.4,${normalize}[wall]`,
    `[4:v]trim=duration=4,${normalize}[end]`,
    '[wall][end]xfade=transition=fade:duration=0.4:offset=5[ending]',
    // Deliberate hard cut: browser playback never implies a live wall update.
    '[opening][ending]concat=n=2:v=1:a=0[base]',
  ];
  lines.forEach(([begin, end], i) => filters.push(
    `[${i ? `cap${i-1}` : 'base'}][${i+7}:v]overlay=0:1070:enable='gte(t,${begin})*lt(t,${end})'[cap${i}]`));
  // Keep the complete end card too; no blank opening or closing poster frame.
  args.push('-filter_complex_threads', '1', '-filter_complex', filters.join(';'), '-map', '[cap3]',
    '-t', '28', '-an', '-c:v', 'libx264', '-threads', '2', '-crf', '22', '-preset', 'medium',
    '-pix_fmt', 'yuv420p', '-force_key_frames', '0', '-map_metadata', '-1', '-movflags', '+faststart', path.join(output, 'inky-walkthrough.mp4'));
  command(ffmpeg, args);
  assert(statSync(path.join(output, 'inky-walkthrough.mp4')).size < 10_000_000, 'MP4 exceeds attachment budget');
  const openingPreview = verifyOpeningFrame(ffmpeg, path.join(output, 'inky-walkthrough.mp4'),
    path.join(output, 'inky-walkthrough-poster.png'));
  command(ffmpeg, ['-hide_banner', '-loglevel', 'error', '-n', '-i', path.join(output, 'inky-walkthrough.mp4'),
    '-filter_complex', '[0:v]fps=5,scale=600:375:flags=lanczos,split[a][b];[a]palettegen=max_colors=96:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=3',
    '-loop', '-1', path.join(output, 'inky-walkthrough-share.gif')]);
  assert(statSync(path.join(output, 'inky-walkthrough-share.gif')).size < 5_000_000, 'GIF exceeds sharing budget');
  command(ffmpeg, ['-hide_banner', '-loglevel', 'error', '-n', '-i',
    path.join(output, 'inky-walkthrough-poster.png'), '-vf', 'scale=1440:900:flags=lanczos',
    '-frames:v', '1', '-q:v', '3', '-map_metadata', '-1', path.join(output, 'inky-walkthrough-poster.jpg')]);
  assert(statSync(path.join(output, 'inky-walkthrough-poster.jpg')).size < 1_000_000, 'Poster exceeds docs budget');
  command(ffmpeg, ['-hide_banner', '-loglevel', 'error', '-n', '-i', path.join(output, 'inky-walkthrough.mp4'),
    '-vf', "select='eq(n,45)+eq(n,135)+eq(n,285)+eq(n,450)+eq(n,660)+eq(n,780)',scale=480:300,tile=3x2",
    '-frames:v', '1', path.join(output, 'review-strip.png')]);
  writeFileSync(path.join(output, 'inky-walkthrough-transcript.md'),
    '# Inky Bird walkthrough\n\nA silent, 28-second film using the real app, two public approved plates and a synthetic Garden birds collection. '
    + 'No personal sightings, production services or account creation appear. UI scenes are labeled Sample data. Navigation is accelerated; automatic browser playback remains once a minute. Nothing uploads automatically.\n\n'
    + '- 0:00–0:02.5: Northern Cardinal artwork with the complete Inky Bird title visible from the first frame. A field journal for the birds you see.\n'
    + '- 0:02.5–0:07: Close view of the approved Northern Cardinal plate.\n'
    + '- 0:07–0:12: Home, Collections and Garden birds in the actual app.\n'
    + '- 0:12–0:19: Frame mode, a real Play action and a real manual Next action to American Robin. Automatic playback changes once a minute; this short clip does not show an automatic transition.\n'
    + '- 0:19–0:24: Hard cut to a photograph of a separate e-paper frame showing Eastern Bluebird. Browser Play does not trigger or synchronize that physical display.\n'
    + '- 0:24–0:28: Gallery and repository links, with a fade to the end card.\n');
  const stamp = value => `00:${value.toFixed(3).padStart(6, '0')}`;
  writeFileSync(path.join(output, 'inky-walkthrough.vtt'), 'WEBVTT\n\n' + [
    [0, 2.5, 'Inky Bird. A field journal for the birds you see.'], ...lines,
    [24, 28, 'Browse the gallery: henrysowell.com/birds. github.com/veteranbv/inky-bird'],
  ].map(([begin, end, text]) => `${stamp(begin)} --> ${stamp(end)}\n${text}\n`).join('\n'));
  const files = ['inky-walkthrough.mp4', 'inky-walkthrough-share.gif', 'inky-walkthrough-poster.png',
    'inky-walkthrough-poster.jpg',
    'inky-walkthrough-transcript.md', 'inky-walkthrough.vtt', 'review-strip.png'];
  writeFileSync(path.join(output, 'media-review.json'), JSON.stringify({ version: 2,
    size: { width: 1920, height: 1200 }, seconds: 28, wallHardCutAt: 19,
    sampleDataVisibleInUI: true, errors, blockedExternalRequests: blocked,
    openingPreview,
    files: Object.fromEntries(files.map(name => [name, { bytes: statSync(path.join(output, name)).size,
      sha256: createHash('sha256').update(readFileSync(path.join(output, name))).digest('hex') }])),
  }, null, 2) + '\n');
  console.log(`Cinematic media: ${output}`);
}

(process.argv.includes('--v2') ? renderCinematic() : render()).catch((error) => {
  console.error(error.message); process.exitCode = 1;
});
