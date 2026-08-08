#!/usr/bin/env node
const assert = require('assert');
const { buildContextMenuTemplate, isExternalUrl } = require('../standalone/context-menu');

for (const value of ['https://example.com', 'http://example.com/path']) {
  assert.strictEqual(isExternalUrl(value), true, value + ' should be allowed');
}
for (const value of ['', 'not a URL', 'file:///tmp/example', 'javascript:alert(1)', 'data:text/plain,test']) {
  assert.strictEqual(isExternalUrl(value), false, value + ' should be rejected');
}

const calls = [];
const actions = {
  openExternal: function(url) { calls.push(['open', url]); },
  copyText: function(text) { calls.push(['copy', text]); },
  copyImage: function(x, y) { calls.push(['image', x, y]); }
};
const editable = buildContextMenuTemplate({
  isEditable: true,
  editFlags: { canUndo: true, canRedo: false, canCut: true, canCopy: true, canPaste: true, canDelete: true, canSelectAll: true }
}, actions);
assert.deepStrictEqual(editable.filter((item) => item.role).map((item) => item.role), [
  'undo', 'redo', 'cut', 'copy', 'paste', 'pasteAndMatchStyle', 'delete', 'selectAll'
]);
assert.strictEqual(editable.find((item) => item.role === 'redo').enabled, false);

const link = buildContextMenuTemplate({ selectionText: 'link', editFlags: { canCopy: true, canSelectAll: true }, linkURL: 'https://example.com/link' }, actions);
assert.ok(link.some((item) => item.label === 'Open Link in Browser'));
link.find((item) => item.label === 'Open Link in Browser').click();
link.find((item) => item.label === 'Copy Link Address').click();

const image = buildContextMenuTemplate({ mediaType: 'image', x: 12, y: 24, srcURL: 'https://example.com/image.png' }, actions);
image.find((item) => item.label === 'Copy Image').click();
image.find((item) => item.label === 'Open Image in Browser').click();
assert.deepStrictEqual(calls, [
  ['open', 'https://example.com/link'],
  ['copy', 'https://example.com/link'],
  ['image', 12, 24],
  ['open', 'https://example.com/image.png']
]);

assert.deepStrictEqual(buildContextMenuTemplate({ linkURL: 'file:///tmp/example' }, actions), []);
assert.deepStrictEqual(buildContextMenuTemplate({}, actions), []);

console.log('context menu tests: PASS');