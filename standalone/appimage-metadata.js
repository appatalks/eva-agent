const updateInformation = 'gh-releases-zsync|appatalks|eva-agent|latest|Eva.Standalone-*.AppImage.zsync';

function releaseAssetName(filename) {
  return filename.replace(/ /g, '.');
}

function releaseDownloadUrl(version, filename) {
  if (!/^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/.test(version)) {
    throw new Error('AppImage release version must be a semantic version.');
  }
  return `https://github.com/appatalks/eva-agent/releases/download/v${version}/${filename}`;
}

module.exports = { updateInformation, releaseAssetName, releaseDownloadUrl };
