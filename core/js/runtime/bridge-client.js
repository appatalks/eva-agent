// Shared private bridge client for browser features that do not own transport.
async function getSettingsBridgeUrl() {
  if (typeof detectACPBridge === 'function') return await detectACPBridge();
  if (typeof getACPBridgeUrl === 'function') return getACPBridgeUrl();
  return 'http://localhost:8888';
}

async function backgroundBridgeRequest(path, options) {
  var bridgeUrl = await getSettingsBridgeUrl();
  var response = await fetch(bridgeUrl.replace(/\/+$/, '') + path, options || {});
  var text = await response.text();
  var data = {};
  if (text) {
    try { data = JSON.parse(text); } catch (_) { data = { message: text }; }
  }
  if (!response.ok) {
    var message = data && data.error && data.error.message ? data.error.message : (data.message || ('HTTP ' + response.status));
    var error = new Error(message);
    error.status = response.status;
    error.data = data;
    throw error;
  }
  return data;
}