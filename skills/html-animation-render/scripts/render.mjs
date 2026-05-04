#!/usr/bin/env node
// Render an HTML/SVG animation page to an MP4 video, optionally muxing in audio.
//
// Usage:
//   node render.mjs --html path/to/page.html --out path/to/out.mp4 \
//                   [--audio path/to/track.mp3] [--duration 33] \
//                   [--width 1920] [--height 853]
//
// Pipeline:
//   1. Playwright headless Chrome records the page (silent webm).
//   2. ffmpeg muxes the audio in (or copies the silent video) → mp4.
//
// The page must drive its own animation on load (CSS animations or rAF).
// The duration you pass is just how long Playwright records — set it to
// the page's full animation length plus a small buffer.
//
// Requires: playwright (project dep), system Chrome, ffmpeg in PATH,
// playwright's bundled ffmpeg (run `npx playwright install ffmpeg` once).

import { chromium } from 'playwright';
import { mkdirSync } from 'node:fs';
import { spawnSync, execSync } from 'node:child_process';
import { resolve } from 'node:path';
import { parseArgs } from 'node:util';

const { values } = parseArgs({
  options: {
    html: { type: 'string' },
    out: { type: 'string' },
    audio: { type: 'string' },
    duration: { type: 'string', default: '33' },
    width: { type: 'string', default: '1920' },
    height: { type: 'string' },
  },
});

if (!values.html || !values.out) {
  console.error('Usage: render.mjs --html <input.html> --out <output.mp4> [--audio <track.mp3>] [--duration 33] [--width 1920] [--height ...]');
  process.exit(1);
}

const HTML = values.html.startsWith('file://') ? values.html : `file://${resolve(values.html)}`;
const OUT = resolve(values.out);
const AUDIO = values.audio ? resolve(values.audio) : null;
const DURATION_S = Number(values.duration);
const WIDTH = Number(values.width);
const HEIGHT = values.height ? Number(values.height) : Math.round(WIDTH / 2.25);
const TMP_DIR = `/tmp/html-animation-render-${Date.now()}`;

mkdirSync(TMP_DIR, { recursive: true });

console.error(`Recording ${WIDTH}x${HEIGHT} for ${DURATION_S}s ...`);

const browser = await chromium.launch({ channel: 'chrome', headless: true });
const context = await browser.newContext({
  viewport: { width: WIDTH, height: HEIGHT },
  recordVideo: { dir: TMP_DIR, size: { width: WIDTH, height: HEIGHT } },
});
const page = await context.newPage();
await page.goto(HTML, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(DURATION_S * 1000);
await page.close();
await context.close();
await browser.close();

const webm = execSync(`ls -t ${TMP_DIR}/*.webm | head -1`).toString().trim();
if (!webm) { console.error('No webm produced'); process.exit(1); }
console.error(`Recorded: ${webm}`);

const ffmpegArgs = ['-y', '-loglevel', 'error', '-i', webm];
if (AUDIO) ffmpegArgs.push('-i', AUDIO, '-map', '0:v', '-map', '1:a');
ffmpegArgs.push(
  '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '20',
);
if (AUDIO) ffmpegArgs.push('-c:a', 'aac', '-b:a', '192k');
ffmpegArgs.push('-t', String(DURATION_S), OUT);

const ff = spawnSync('ffmpeg', ffmpegArgs, { stdio: 'inherit' });
if (ff.status !== 0) { console.error('ffmpeg failed'); process.exit(ff.status); }
console.log(OUT);
