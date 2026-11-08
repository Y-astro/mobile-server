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
const { spawnSync, execFileSync } = require('child_process');

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

function parseArgs() {
  const args = process.argv.slice(2);
  const opts = {
    limit: 2,
    stagingDir: path.resolve(process.cwd(), '.work/mangafire-covers'),
    dryRun: false,
    titles: null,
  };

  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (a === '--limit' && i + 1 < args.length) {
      opts.limit = parseInt(args[++i], 10);
    } else if (a === '--staging-dir' && i + 1 < args.length) {
      opts.stagingDir = path.resolve(args[++i]);
    } else if (a === '--dry-run') {
      opts.dryRun = true;
    } else if (a === '--titles' && i + 1 < args.length) {
      opts.titles = args[++i].split(',').map(s => parseInt(s.trim(), 10)).filter(n => !isNaN(n));
    } else if (a === '-h' || a === '--help') {
      console.log('Usage: node tools/collect_mangafire_covers.cjs [options]');
      console.log('  --limit <N>         Max covers to stage (default: 2)');
      console.log('  --staging-dir <dir> Destination staging dir (default: .work/mangafire-covers)');
      console.log('  --dry-run           Check library status without launching browser');
      console.log('  --titles <id,id>    Specific manga IDs to collect');
      process.exit(0);
    }
  }
  return opts;
}

function fetchMissingTitlesFromPhone() {
  const remoteScript = `
import json, os, urllib.request
from pathlib import Path

req = urllib.request.Request(
    'http://127.0.0.1:4567/suwayomi/api/graphql',
    data=json.dumps({'query': 'query { mangas { nodes { id title url inLibrary source { id name } } } }'}).encode(),
    headers={'Content-Type': 'application/json'}
)
with urllib.request.urlopen(req, timeout=10) as resp:
    data = json.load(resp)

mangas = data.get('data', {}).get('mangas', {}).get('nodes', [])
mf = [m for m in mangas if (m.get('source') or {}).get('id') == '6084907896154116083' and m.get('inLibrary')]

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
        missing.append({'id': m['id'], 'title': m['title'], 'url': m['url']})

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
    throw new Error(`Failed to query phone GraphQL: ${res.stderr || res.stdout}`);
  }

  return JSON.parse(res.stdout.trim());
}

function parseHidFromUrl(url) {
  // Stored URLs:
  // e.g. "/manga/paranoid-magee.3pljr" -> hid: "3pljr", slug: "paranoid-magee"
  // e.g. "/title/3pljr-paranoid-magee" -> hid: "3pljr", slug: "paranoid-magee"
  const clean = url.replace(/^\/+/, '');
  if (clean.startsWith('manga/')) {
    const rest = clean.slice('manga/'.length);
    const lastDot = rest.lastIndexOf('.');
    if (lastDot !== -1) {
      const slug = rest.slice(0, lastDot);
      const hid = rest.slice(lastDot + 1);
      return { hid, slug, publicUrl: `https://mangafire.to/title/${hid}-${slug}` };
    }
  } else if (clean.startsWith('title/')) {
    const rest = clean.slice('title/'.length);
    const firstDash = rest.indexOf('-');
    if (firstDash !== -1) {
      const hid = rest.slice(0, firstDash);
      const slug = rest.slice(firstDash + 1);
      return { hid, slug, publicUrl: `https://mangafire.to/title/${rest}` };
    }
  }
  return { hid: null, slug: null, publicUrl: `https://mangafire.to/${clean}` };
}

function downloadImageBytes(url) {
  return new Promise((resolve, reject) => {
    https.get(url, { headers: { 'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64)' } }, res => {
      if (res.statusCode !== 200) {
        return reject(new Error(`Image fetch failed with status ${res.statusCode} from ${url}`));
      }
      const chunks = [];
      res.on('data', chunk => chunks.push(chunk));
      res.on('end', () => {
        const buf = Buffer.concat(chunks);
        resolve({
          statusCode: res.statusCode,
          contentType: res.headers['content-type'],
          bytes: buf,
        });
      });
    }).on('error', reject);
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
    print(json.dumps({'format': fmt, 'width': w, 'height': h}))
except Exception as e:
    sys.exit(3)
`;

  const res = spawnSync('python3', ['-c', pyCode], {
    input: imgBuffer,
    encoding: 'utf8',
    maxBuffer: 1024 * 1024,
  });

  if (res.status !== 0) {
    throw new Error(`Pillow verification failed with code ${res.status}: ${res.stderr || ''}`);
  }

  return JSON.parse(res.stdout.trim());
}

async function main() {
  const opts = parseArgs();

  console.log('=== MANGAFIRE COVER RECOVERY COLLECTOR ===');
  console.log('Reading library state from phone...');
  const phoneState = fetchMissingTitlesFromPhone();

  console.log(`MangaFire in-library total: ${phoneState.total_mf}`);
  console.log(`Already cached on phone:   ${phoneState.cached_count}`);
  console.log(`Missing thumbnails:        ${phoneState.missing_count}`);

  if (opts.dryRun) {
    console.log('\nDRY-RUN mode requested. Top missing titles:');
    phoneState.missing.slice(0, 10).forEach(m => {
      console.log(`  [ID ${m.id}] ${m.title} (${m.url})`);
    });
    return;
  }

  let candidates = phoneState.missing;
  if (opts.titles && opts.titles.length > 0) {
    candidates = candidates.filter(m => opts.titles.includes(m.id));
  } else if (opts.limit && opts.limit > 0) {
    candidates = candidates.slice(0, opts.limit);
  }

  if (candidates.length === 0) {
    console.log('No eligible missing covers to collect.');
    return;
  }

  console.log(`\nCollecting covers for ${candidates.length} candidate titles...`);

  // Setup staging directory with private permissions
  fs.mkdirSync(opts.stagingDir, { recursive: true, mode: 0o700 });
  const imagesDir = path.join(opts.stagingDir, 'images');
  fs.mkdirSync(imagesDir, { recursive: true, mode: 0o700 });

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1280, height: 800 },
    userAgent: 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
  });

  const manifestEntries = [];

  try {
    for (const cand of candidates) {
      const mangaId = cand.id;
      const { hid, publicUrl } = parseHidFromUrl(cand.url);

      if (!hid) {
        console.warn(`[WARN] Skipping ID ${mangaId}: could not parse hid from url ${cand.url}`);
        continue;
      }

      console.log(`\nProcessing [ID ${mangaId}] ${cand.title} (hid: ${hid})...`);
      const page = await context.newPage();

      // Block heavy media/fonts/images to save bandwidth during metadata acquisition
      await page.route('**/*', route => {
        const type = route.request().resourceType();
        if (['image', 'media', 'font'].includes(type)) {
          return route.abort();
        }
        return route.continue();
      });

      let titleApiData = null;
      page.on('response', async res => {
        const u = res.url();
        if (u.includes(`/api/titles/${hid}?vrf=`)) {
          try {
            titleApiData = await res.json();
          } catch (e) {}
        }
      });

      try {
        await page.goto(publicUrl, { waitUntil: 'networkidle', timeout: 25000 });
      } catch (err) {
        console.warn(`  Navigation completed with notice: ${err.message}`);
      }

      // Check for challenge markers
      const pageTitle = await page.title().catch(() => '');
      if (pageTitle.toLowerCase().includes('just a moment') || pageTitle.toLowerCase().includes('attention required')) {
        console.error(`[ABORT] Access challenge / Cloudflare Turnstile detected on ${publicUrl}. Aborting batch.`);
        await page.close();
        break;
      }

      if (!titleApiData || !titleApiData.data || !titleApiData.data.poster) {
        console.warn(`  Failed to capture /api/titles/${hid} metadata response.`);
        await page.close();
        continue;
      }

      const posterObj = titleApiData.data.poster;
      const canonicalUrl = posterObj.large || posterObj.medium || posterObj.small;
      if (!canonicalUrl || !canonicalUrl.startsWith('https://static.mfcdn.')) {
        console.warn(`  Invalid canonical cover URL: ${canonicalUrl}`);
        await page.close();
        continue;
      }

      console.log(`  Canonical cover: ${canonicalUrl}`);

      // Direct HTTPS fetch of image bytes
      const imgRes = await downloadImageBytes(canonicalUrl);
      console.log(`  Downloaded: ${imgRes.bytes.length} bytes, content-type: ${imgRes.contentType}`);

      // Pillow decode & dimension verification
      const pilMeta = verifyImageWithPillow(imgRes.bytes);
      console.log(`  Pillow check: format=${pilMeta.format}, size=${pilMeta.width}x${pilMeta.height}`);

      // Write private staged image
      const outFilename = `images/${mangaId}.jpg`;
      const outPath = path.join(opts.stagingDir, outFilename);
      fs.writeFileSync(outPath, imgRes.bytes, { mode: 0o600 });

      const sha256 = crypto.createHash('sha256').update(imgRes.bytes).digest('hex');

      manifestEntries.push({
        manga_id: mangaId,
        hid: hid,
        canonical_url: canonicalUrl,
        filename: outFilename,
        sha256: sha256,
        bytes: imgRes.bytes.length,
        width: pilMeta.width,
        height: pilMeta.height,
        format: pilMeta.format,
      });

      await page.close();
    }
  } finally {
    await browser.close();
  }

  if (manifestEntries.length === 0) {
    console.error('No covers were successfully collected.');
    process.exit(1);
  }

  const manifest = {
    schema_version: 1,
    created_at: new Date().toISOString(),
    source_name: 'MangaFire',
    source_id: '6084907896154116083',
    entries: manifestEntries,
  };

  const manifestPath = path.join(opts.stagingDir, 'manifest.json');
  fs.writeFileSync(manifestPath, JSON.stringify(manifest, null, 2), { mode: 0o600 });

  console.log('\n=== COLLECTION SUMMARY ===');
  console.log(`Staged covers count: ${manifestEntries.length}`);
  console.log(`Manifest path:       ${manifestPath}`);
  manifestEntries.forEach(e => {
    console.log(`  - Manga ID ${e.manga_id} (${e.hid}): ${e.bytes} bytes, SHA256: ${e.sha256}`);
  });
}

main().catch(err => {
  console.error('Fatal collector error:', err);
  process.exit(1);
});
