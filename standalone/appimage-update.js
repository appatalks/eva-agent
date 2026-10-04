const { execFile } = require('node:child_process');
const { open } = require('node:fs/promises');
const path = require('node:path');
const { promisify } = require('node:util');
const { appendBlockmap } = require('app-builder-lib/out/targets/differentialUpdateInfoBuilder');
const { updateInformation, releaseAssetName, releaseDownloadUrl } = require('./appimage-metadata');

const run = promisify(execFile);

/** @param {import('app-builder-lib').ArtifactCreated} event */
module.exports = async function appImageUpdate(event) {
  if (!event.file || !event.file.endsWith('.AppImage')) return;

  const image = event.file;
  const publishedName = releaseAssetName(path.basename(image));
  const zsyncName = `${publishedName}.zsync`;
  const zsyncFile = path.join(path.dirname(image), zsyncName);
  const version = event.packager.appInfo.version;
  const downloadUrl = releaseDownloadUrl(version, publishedName);
  const { stdout: sections } = await run('readelf', ['--wide', '--section-headers', image]);
  const section = sections.match(/^\s*\[\s*\d+\]\s+\.upd_info\s+PROGBITS\s+[0-9a-f]+\s+([0-9a-f]+)\s+([0-9a-f]+)\s/mi);
  if (!section) throw new Error('AppImage runtime has no .upd_info section.');

  const offset = Number.parseInt(section[1], 16);
  const capacity = Number.parseInt(section[2], 16);
  const metadata = Buffer.from(updateInformation, 'utf8');
  const { stdout: runtimeOffset } = await run(image, ['--appimage-offset']);
  const filesystemOffset = Number(runtimeOffset.trim());
  if (!Number.isSafeInteger(filesystemOffset) || filesystemOffset <= 0 ||
      offset + capacity > filesystemOffset || metadata.length >= capacity) {
    throw new Error('AppImage update information does not fit inside the runtime.');
  }

  const file = await open(image, 'r+');
  try {
    const { size } = await file.stat();
    const blockMapSize = event.updateInfo?.blockMapSize;
    if (!Number.isSafeInteger(blockMapSize) || blockMapSize <= 0 ||
        size !== event.updateInfo.size || blockMapSize + 4 >= size - filesystemOffset) {
      throw new Error('AppImage embedded blockmap metadata is invalid.');
    }
    const footer = Buffer.alloc(4);
    const { bytesRead } = await file.read(footer, 0, footer.length, size - footer.length);
    if (bytesRead !== footer.length || footer.readUInt32BE() !== blockMapSize) {
      throw new Error('AppImage embedded blockmap footer does not match its metadata.');
    }

    const data = Buffer.alloc(capacity);
    metadata.copy(data);
    const { bytesWritten } = await file.write(data, 0, data.length, offset);
    if (bytesWritten !== data.length) throw new Error('AppImage update information write was incomplete.');
    // Changing the runtime invalidates electron-builder's already appended blockmap.
    await file.truncate(size - blockMapSize - footer.length);
  } finally {
    await file.close();
  }

  const { stdout: embedded } = await run(image, ['--appimage-updateinformation']);
  if (embedded.trim() !== updateInformation) {
    throw new Error('AppImage runtime could not read the embedded update information.');
  }
  event.updateInfo = await appendBlockmap(image);
  event.safeArtifactName = publishedName;

  try {
    await run('zsyncmake', ['-f', publishedName, '-u', downloadUrl, '-o', zsyncFile, image]);
  } catch (error) {
    if (error.code === 'ENOENT') {
      throw new Error('zsyncmake is required to build AppImage updates; install the zsync package.', { cause: error });
    }
    throw error;
  }
  await event.packager.info.emitArtifactCreated({
    file: zsyncFile,
    safeArtifactName: zsyncName,
    arch: event.arch,
    target: event.target,
    packager: event.packager
  });
};
