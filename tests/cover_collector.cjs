const assert = require('node:assert/strict');
const {test} = require('node:test');
const {parseArgs, parseHidFromUrl, validateCanonicalUrl} = require('../tools/collect_mangafire_covers.cjs');
test('source paths normalize only known public title shapes', () => {
  assert.equal(parseHidFromUrl('/manga/paranoid-magee.3pljr').publicUrl, 'https://mangafire.to/title/3pljr-paranoid-magee');
  assert.equal(parseHidFromUrl('/title/3pljr-paranoid-magee').hid, '3pljr');
  for (const path of ['https://evil.test/title/x', '//evil.test/x', '/manga/../../secret.x', '/other/path', null]) assert.equal(parseHidFromUrl(path).hid, null);
});
test('canonical images keep exact HTTPS CDN host', () => {
  assert.equal(validateCanonicalUrl('https://static.mfcdn.nl/e357/i/2/2a/hash@280.jpg'), 'https://static.mfcdn.nl/e357/i/2/2a/hash@280.jpg');
  for (const url of ['http://static.mfcdn.nl/x.jpg', 'https://static.mfcdn.nl.evil.test/x.jpg', 'https://s.mfcdn.nl/x.jpg', 'https://a@static.mfcdn.nl/x.jpg', 'https://static.mfcdn.nl/x.jpg?token=x', 'https://static.mfcdn.nl/x.svg']) assert.throws(() => validateCanonicalUrl(url));
});
test('collection bounds and arguments fail closed', () => {
  assert.deepEqual(parseArgs(['--limit','128','--titles','107,83']).titles, [107,83]);
  for (const args of [['--limit','0'], ['--limit','-1'], ['--limit','129'], ['--limit','2x'], ['--titles','1,no'], ['--unknown'], ['--staging-dir']]) assert.throws(() => parseArgs(args));
});
