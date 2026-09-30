const assert = require('node:assert/strict');
const { execFile } = require('node:child_process');
const { createHash } = require('node:crypto');
const { createReadStream } = require('node:fs');
const { glob, mkdtemp, open, readFile, readdir, rename, rm, writeFile } = require('node:fs/promises');
const { createRequire } = require('node:module');
const os = require('node:os');
const path = require('node:path');
const { promisify } = require('node:util');
const { inflateRawSync } = require('node:zlib');
const { updateInformation, releaseAssetName, releaseDownloadUrl } = require('../../standalone/appimage-metadata');

const root = path.resolve(__dirname, '../..');
const config = require('../../standalone/package.json');
const builderRequire = createRequire(require.resolve('app-builder-lib/package.json', { paths: [path.join(root, 'standalone')] }));
const { blake2b } = builderRequire('@noble/hashes/blake2.js');
const run = promisify(execFile);

async function fileHashes(filename) {
  const sha1 = createHash('sha1');
  const sha256 = createHash('sha256');
  for await (const chunk of createReadStream(filename)) {
    sha1.update(chunk);
    sha256.update(chunk);
  }
  return { sha1: sha1.digest('hex'), sha256: sha256.digest('hex') };
}

async function checkNativePayload(directory) {
  const files = await readdir(directory, { withFileTypes: true });
  let count = 0;
  for (const entry of files) {
    const filename = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      count += await checkNativePayload(filename);
      continue;
    }
    if (!entry.isFile()) continue;
    const file = await open(filename, 'r');
    let elf;
    try {
      const header = Buffer.alloc(4);
      await file.read(header, 0, 4, 0);
      elf = header.equals(Buffer.from([0x7f, 0x45, 0x4c, 0x46]));
    } finally {
      await file.close();
    }
    if (!elf) continue;
    const { stdout } = await run('readelf', ['--version-info', filename]);
    for (const version of stdout.matchAll(/\bGLIBC_(\d+)\.(\d+)\b/g)) {
      const major = Number(version[1]);
      const minor = Number(version[2]);
      assert.ok(major < 2 || (major === 2 && minor <= 35),
        `${path.relative(directory, filename)} requires glibc newer than the supported 2.35 baseline.`);
    }
    count += 1;
  }
  return count;
}

async function verifyAppImage(filename, version) {
  const publishedName = releaseAssetName(path.basename(filename));
  const [{ stdout: embedded }, { stdout: offset }, { stdout: dynamic }, { stdout: programs }] = await Promise.all([
    run(filename, ['--appimage-updateinformation']),
    run(filename, ['--appimage-offset']),
    run('readelf', ['--wide', '--dynamic', filename]),
    run('readelf', ['--wide', '--program-headers', filename])
  ]);
  assert.equal(embedded.trim(), updateInformation, 'AppImage update information must match the release feed.');
  assert.doesNotMatch(dynamic, /\(NEEDED\)/, 'AppImage launcher must not need host shared libraries.');
  assert.doesNotMatch(programs, /\bINTERP\b/, 'AppImage launcher must not need a host ELF interpreter.');
  const filesystemOffset = Number(offset.trim());
  assert.ok(Number.isSafeInteger(filesystemOffset) && filesystemOffset > 0);

  const hashes = await fileHashes(filename);
  const sidecar = await readFile(path.join(path.dirname(filename), publishedName + '.zsync'));
  const headerEnd = sidecar.indexOf('\n\n');
  assert.ok(headerEnd > 0 && headerEnd < 16384, 'zsync control header must be bounded and present.');
  const header = sidecar.subarray(0, headerEnd).toString();
  const fields = Object.fromEntries(header.split('\n').map(line => {
    const split = line.indexOf(': ');
    return [line.slice(0, split), line.slice(split + 2)];
  }));
  assert.equal(fields.Filename, publishedName);
  assert.equal(fields.URL, releaseDownloadUrl(version, publishedName));
  assert.equal(fields['SHA-1'], hashes.sha1, 'zsync checksum must match the final binary.');

  const file = await open(filename, 'r');
  try {
    const { size } = await file.stat();
    assert.equal(fields.Length, String(size));
    const footer = Buffer.alloc(4);
    assert.equal((await file.read(footer, 0, 4, size - 4)).bytesRead, 4);
    const mapSize = footer.readUInt32BE();
    assert.ok(mapSize > 0 && mapSize <= 16 * 1024 * 1024 && mapSize + 4 < size - filesystemOffset);
    const mapData = Buffer.alloc(mapSize);
    assert.equal((await file.read(mapData, 0, mapSize, size - mapSize - 4)).bytesRead, mapSize);
    const map = JSON.parse(inflateRawSync(mapData, { maxOutputLength: 64 * 1024 * 1024 }));
    assert.equal(map.version, '2');
    assert.equal(map.files.length, 1);
    const blocks = map.files[0];
    assert.equal(blocks.offset, 0);
    assert.equal(blocks.sizes.length, blocks.checksums.length);
    let position = 0;
    for (let index = 0; index < blocks.sizes.length; index++) {
      const length = blocks.sizes[index];
      assert.ok(Number.isSafeInteger(length) && length > 0 && length <= 32768);
      const chunk = Buffer.alloc(length);
      assert.equal((await file.read(chunk, 0, length, position)).bytesRead, length);
      assert.equal(Buffer.from(blake2b(chunk, { dkLen: 18 })).toString('base64'), blocks.checksums[index],
        'Embedded Electron blockmap must match every byte of the final payload.');
      position += length;
    }
    assert.equal(position, size - mapSize - 4);
  } finally {
    await file.close();
  }

  const extracted = await mkdtemp(path.join(os.tmpdir(), 'eva-release-check-'));
  let nativeCount;
  try {
    await run(filename, ['--appimage-extract'], { cwd: extracted, maxBuffer: 16 * 1024 * 1024 });
    const payload = path.join(extracted, 'squashfs-root');
    const desktop = await readFile(path.join(payload, 'Eva.desktop'), 'utf8');
    assert.match(desktop, /^Name=Eva$/m);
    assert.match(desktop, /^StartupWMClass=Eva$/m);
    assert.match(desktop, /^Exec=AppRun --eva-workspace-terminal-v1 %U$/m);
    nativeCount = await checkNativePayload(payload);
    assert.ok(nativeCount > 0, 'Native payload must be present for compatibility inspection.');
    const terminals = [];
    for await (const terminal of glob('resources/app.asar.unpacked/node_modules/node-pty/**/*.node',
      { cwd: payload })) terminals.push(terminal);
    assert.ok(terminals.length > 0, 'Default coding workspaces require the unpacked native terminal module.');
  } finally {
    await rm(extracted, { recursive: true });
  }
  return { ...hashes, nativeCount };
}

async function main() {
  const version = config.version;
  const tag = process.env.GITHUB_REF_TYPE === 'tag' ? process.env.GITHUB_REF_NAME : '';
  if (tag) assert.equal(tag, 'v' + version, 'Release tag must match standalone/package.json.');
  const directory = process.env.EVA_APPIMAGE_DIST || path.join(root, 'standalone/dist');
  const filename = path.join(directory, `Eva Standalone-${version}.AppImage`);
  const hashes = await verifyAppImage(filename, version);
  const publishedName = releaseAssetName(path.basename(filename));
  if (process.argv.includes('--release')) await rename(filename, path.join(directory, publishedName));
  const checksumName = process.argv.includes('--release') ? publishedName : path.basename(filename);
  const sidecarName = publishedName + '.zsync';
  const sidecarHash = (await fileHashes(path.join(directory, sidecarName))).sha256;
  await writeFile(path.join(directory, 'SHA256SUMS'),
    `${hashes.sha256}  ${checksumName}\n${sidecarHash}  ${sidecarName}\n`);
  console.log(`Verified AppImage identity, static runtime, update control file, blockmap, and ${hashes.nativeCount} bundled native binaries (glibc <= 2.35).`);
}

module.exports = { verifyAppImage, checkNativePayload, fileHashes };
if (require.main === module) {
  main().catch(error => { console.error(error.message); process.exitCode = 1; });
}
