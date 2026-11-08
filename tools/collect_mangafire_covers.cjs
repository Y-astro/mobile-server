#!/usr/bin/env node
/**
 * collect_mangafire_covers.cjs
 *
 * Host-only collector for missing MangaFire library covers.
 * Uses headless Chromium (Playwright) to capture canonical cover URLs from the
 * normal SPA API response (/api/titles/<hid>?vrf=...), downloads the image directly
 * via HTTPS, verifies byte integrity and Pillow decoding, and stages to private storage.
 *
 * NEVER performs live writes, DB updates, or production mutations.
 */

'use strict';

const fs = require('fs');
const path = require('path');
const https = require('https');
const crypto = require('crypto');
const { spawnSync } = require('child_process');

let chromium;
try {
  ({ chromium } = require('/home/astro/repos/mobile-server/node_modules/@playwright/test'));
} catch (e) {
  try {
    ({ chromium } = require('@playwright/test'));
  } catch (e2) {
    console.error('ERROR: Playwright chromium not found.');
    process.exit(1);
  }
}

const SSH_TARGET = 'u0_a505@100.75.159.53';
const SSH_PORT = '8022';

function parseArgs(args = process.argv.slice(2)) {
  const opts = {limit: 2, stagingDir: path.resolve('.work/mangafire-covers'), dryRun: false, titles: null};
  const positive = value => {
    if (!/^[0-9]+$/.test(value || '') || !Number.isSafeInteger(Number(value)) || Number(value) < 1 || Number(value) >= 2 ** 31) {
      throw new Error('Expected a positive integer');
    }
    return Number(value);
  };
  for (let i = 0; i < args.length; i++) {
    switch (args[i]) {
      case '--limit': opts.limit = positive(args[++i]); break;
      case '--staging-dir':
        if (!args[i + 1] || args[i + 1].startsWith('--')) throw new Error('Missing staging directory');
        opts.stagingDir = path.resolve(args[++i]); break;
      case '--dry-run': opts.dryRun = true; break;
      case '--titles': opts.titles = (args[++i] || '').split(',').map(positive); break;
      case '--help': case '-h':
        console.log('Usage: node tools/collect_mangafire_covers.cjs [--limit 1..128] [--titles id,id] [--staging-dir NEW_DIR] [--dry-run]');
        process.exit(0); break;
      default: throw new Error('Unknown collector argument');
    }
  }
  if (opts.limit > 128) throw new Error('At most 128 covers per batch');
  return opts;
}

function fetchMissingTitlesFromPhone() {
  const remoteScript = `
import json, os, urllib.request
from pathlib import Path

req = urllib.request.Request(
    'http://127.0.0.1:4567/suwayomi/api/graphql',
    data=json.dumps({'query': 'query { mangas(condition: {inLibrary: true}) { nodes { id url source { id name } } } }'}).encode(),
    headers={'Content-Type': 'application/json'}
)
with urllib.request.urlopen(req, timeout=10) as resp:
    data = json.load(resp)

assert not data.get('errors'), 'GraphQL request failed'
mangas = data['data']['mangas']['nodes']
mf = [m for m in mangas if str((m.get('source') or {}).get('id')) == '6084907896154116083']

td = Path.home() / 'suwayomi/data/downloads/thumbnails'
cached_files = set(os.listdir(td)) if td.exists() and td.is_dir() else set()

total_mf = len(mf)
cached = []
missing = []

for m in mf:
    mid = str(m['id'])
    has_thumb = any(f'{mid}.{ext}' in cached_files for ext in ['webp', 'jpg', 'jpeg', 'png', 'avif'])
    if has_thumb:
        cached.append(m['id'])
    else:
        missing.append({'id': m['id'], 'url': m['url']})

out = {
    'total_mf': total_mf,
    'cached_count': len(cached),
    'missing_count': len(missing),
    'missing': missing
}
print(json.dumps(out))
`;

  const res = spawnSync(
    'ssh',
    ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', '-p', SSH_PORT, SSH_TARGET, 'python3 -'],
    { input: remoteScript, encoding: 'utf8', maxBuffer: 10 * 1024 * 1024, timeout: 15000 }
  );

  if (res.status !== 0) {
    throw new Error(`Phone GraphQL query failed (exit ${res.status})`);
  }

  return JSON.parse(res.stdout.trim());
}

function parseHidFromUrl(url) {
  // Stored URLs:
  // e.g. "/manga/paranoid-magee.3pljr" -> hid: "3pljr", slug: "paranoid-magee"
  // e.g. "/title/3pljr-paranoid-magee" -> hid: "3pljr", slug: "paranoid-magee"
  if (typeof url !== 'string' || url.length > 512) return {hid: null};
  let match = /^\/manga\/([a-zA-Z0-9_-]+)\.([a-zA-Z0-9_-]{1,64})$/.exec(url);
  if (match) return {hid: match[2], slug: match[1], publicUrl: `https://mangafire.to/title/${match[2]}-${match[1]}`};
  match = /^\/title\/([a-zA-Z0-9]{1,64})-([a-zA-Z0-9_-]+)$/.exec(url);
  return match ? {hid: match[1], slug: match[2], publicUrl: `https://mangafire.to${url}`} : {hid: null};
}

function validateCanonicalUrl(value) {
  const u = new URL(value);
  if (u.protocol !== 'https:' || u.host !== 'static.mfcdn.nl' || u.username || u.password || u.search || u.hash ||
      !/^\/[a-zA-Z0-9/_-]+(?:@[1-9][0-9]{0,3})?\.jpe?g$/.test(u.pathname) || u.href.length > 2048) {
    throw new Error('Invalid canonical JPEG URL');
  }
  return u.href;
}

function downloadImageBytes(value) {
  const url = validateCanonicalUrl(value);
  return new Promise((resolve, reject) => {
    let timer, settled = false;
    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      error ? reject(error) : resolve(value);
    };
    const request = https.get(url, res => {
      const contentType = (res.headers['content-type'] || '').split(';')[0].toLowerCase();
      if (res.statusCode !== 200 || !['image/jpeg', 'image/jpg'].includes(contentType)) {
        res.destroy();
        return finish(new Error('CDN did not return HTTP200 JPEG'));
      }
      const chunks = []; let size = 0;
      res.on('data', chunk => {
        size += chunk.length;
        if (size > 4 * 1024 * 1024) {
          request.destroy(new Error('Image exceeds 4MiB limit'));
          return;
        }
        chunks.push(chunk);
      });
      res.on('end', () => finish(null, {bytes: Buffer.concat(chunks), contentType}));
      res.on('error', error => finish(error));
    });
    request.on('error', error => finish(error));
    timer = setTimeout(() => request.destroy(new Error('Image download deadline exceeded')), 20000);
  });
}

function verifyImageWithPillow(imgBuffer) {
  const pyCode = `
import sys, io, json
from PIL import Image

data = sys.stdin.buffer.read()
if len(data) < 16:
    sys.exit(1)

try:
    img = Image.open(io.BytesIO(data))
    img.verify()
    # Re-open for size check
    img2 = Image.open(io.BytesIO(data))
    w, h = img2.size
    fmt = img2.format
    if w < 50 or h < 50 or w > 4000 or h > 4000:
        sys.exit(2)
    if fmt != 'JPEG':
        sys.exit(4)
    img2.load()
    print(json.dumps({'format': fmt, 'width': w, 'height': h}))
except Exception as e:
    sys.exit(3)
`;

  const res = spawnSync('python3', ['-c', pyCode], {
    input: imgBuffer,
    encoding: 'utf8',
    maxBuffer: 1024 * 1024,
    timeout: 10000,
  });

  if (res.status !== 0) {
    throw new Error(`Pillow verification failed with code ${res.status}: ${res.stderr || ''}`);
  }

  return JSON.parse(res.stdout.trim());
}

async function main() {
  const opts = parseArgs();
  const phoneState = fetchMissingTitlesFromPhone();
  console.log(JSON.stringify({library: phoneState.total_mf, cached: phoneState.cached_count, missing: phoneState.missing_count}));
  if (opts.dryRun) return;
  const candidates = phoneState.missing.filter(m => !opts.titles || opts.titles.includes(m.id)).slice(0, opts.limit);
  if (!candidates.length) { console.log('No missing covers'); return; }
  // Setup staging directory with private permissions
  fs.mkdirSync(path.dirname(opts.stagingDir), {recursive: true, mode: 0o700});
  if (fs.realpathSync(path.dirname(opts.stagingDir)) !== path.dirname(opts.stagingDir)) throw new Error('Staging parent symlink rejected');
  fs.mkdirSync(opts.stagingDir, {mode: 0o700});
  fs.mkdirSync(path.join(opts.stagingDir, 'images'), {mode: 0o700});
  const browser = await chromium.launch({headless: true});
  const context = await browser.newContext({viewport: {width: 1280, height: 800}});
  const manifestEntries = [], failures = [];
  let blocked = false;
  try {
    for (const cand of candidates) {
      const mangaId = cand.id;
      const {hid, publicUrl} = parseHidFromUrl(cand.url);
      if (!Number.isInteger(mangaId) || mangaId < 1 || mangaId >= 2 ** 31 || !hid) throw new Error('Invalid source manga identity');
      const page = await context.newPage();
      try {
        // Block heavy media/fonts/images to save bandwidth during metadata acquisition
        await page.route('**/*', route => ['image', 'media', 'font'].includes(route.request().resourceType()) ? route.abort() : route.continue());
        const response = page.waitForResponse(res => {
          const u = new URL(res.url());
          return u.origin === 'https://mangafire.to' && u.pathname === `/api/titles/${hid}`;
        }, {timeout: 15000}).catch(() => null);
        await page.goto(publicUrl, {waitUntil: 'domcontentloaded', timeout: 15000});
        const api = await response;
        // Check for challenge markers
        const title = (await page.title()).toLowerCase();
        const humanCheck = await page.locator('iframe[src*="challenges.cloudflare.com"]').count();
        if (title.includes('just a moment') || title.includes('attention required') || humanCheck || api?.status() === 403) {
          blocked = true;
          break;
        }
        if (!api || api.status() !== 200) throw new Error('Current title metadata unavailable');
        const text = await api.text();
        if (text.length > 2 * 1024 * 1024) throw new Error('Metadata exceeds size limit');
        const titleApiData = JSON.parse(text);
        if (titleApiData.data?.hid !== hid || !titleApiData.data.poster) throw new Error('Metadata title identity mismatch');
        const posterObj = titleApiData.data.poster;
        const canonicalUrl = validateCanonicalUrl(posterObj.large || posterObj.medium || posterObj.small);
        // Direct HTTPS fetch of image bytes
        const imgRes = await downloadImageBytes(canonicalUrl);
        // Pillow decode & dimension verification
        const pilMeta = verifyImageWithPillow(imgRes.bytes);
        // Write private staged image
        const outFilename = `images/${mangaId}.jpg`;
        fs.writeFileSync(path.join(opts.stagingDir, outFilename), imgRes.bytes, {mode: 0o600, flag: 'wx'});
        manifestEntries.push({manga_id: mangaId, hid, canonical_url: canonicalUrl, filename: outFilename,
          sha256: crypto.createHash('sha256').update(imgRes.bytes).digest('hex'), bytes: imgRes.bytes.length,
          width: pilMeta.width, height: pilMeta.height, format: pilMeta.format});
        console.log(`Staged ${manifestEntries.length}/${candidates.length}`);
      } catch (error) {
        failures.push({manga_id: mangaId, error: error.message.split('\n')[0]});
        console.log(`Skipped one unavailable cover (${failures.length} failures)`);
      } finally {
        await page.close();
      }
      await new Promise(resolve => setTimeout(resolve, 600));
    }
  } finally {
    await browser.close();
    const manifest = {schema_version: 1, created_at: new Date().toISOString(), source_name: 'MangaFire',
      source_id: '6084907896154116083', entries: manifestEntries};
    fs.writeFileSync(path.join(opts.stagingDir, 'manifest.json'), JSON.stringify(manifest, null, 2), {mode: 0o600, flag: 'wx'});
  }
  console.log(JSON.stringify({requested: candidates.length, staged: manifestEntries.length, failures, blocked}));
  if (!manifestEntries.length || failures.length || blocked) process.exitCode = 2;
}

module.exports = {parseArgs, parseHidFromUrl, validateCanonicalUrl};
if (require.main === module) main().catch(error => { console.error('Collector failed:', error.message); process.exitCode = 1; });
