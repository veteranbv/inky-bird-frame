const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');

function assertOpeningPixels(actual, poster) {
  assert.equal(actual.length, 64 * 40, 'Opening preview must decode completely');
  assert.equal(poster.length, actual.length, 'Poster must decode at the same size');
  const mean = poster.reduce((sum, value) => sum + value, 0) / poster.length;
  const contrast = Math.sqrt(poster.reduce((sum, value) => sum + (value - mean) ** 2, 0) / poster.length);
  assert(contrast > 10, 'Opening poster is blank or lacks title contrast');
  const meanError = actual.reduce((sum, value, i) => sum + Math.abs(value - poster[i]), 0) / actual.length;
  assert(meanError < 4, 'First decoded frame does not match the complete opening poster');
  return { contrast, meanError };
}

function verifyOpeningFrame(ffmpeg, video, poster) {
  const decode = (file) => {
    const result = spawnSync(ffmpeg, ['-hide_banner', '-loglevel', 'error', '-i', file,
      '-vf', 'scale=64:40:flags=area,format=gray', '-frames:v', '1', '-f', 'rawvideo', 'pipe:1']);
    if (result.status !== 0) throw new Error(result.stderr.toString() || result.error?.message);
    return result.stdout;
  };
  return assertOpeningPixels(decode(video), decode(poster));
}

module.exports = { assertOpeningPixels, verifyOpeningFrame };
