// Proactive notification polling, rendering, voice batching, and acknowledgment.
var _notifState = { polling: false, timer: null, intervalMs: 60000 };

function injectProactiveBubble(notification) {
  var txtOutput = document.getElementById('txtOutput');
  if (!txtOutput) return;
  if (typeof hideEvaWelcome === 'function') hideEvaWelcome();
  var title = escapeHtml(String(notification.title || 'Eva'));
  var body = escapeHtml(String(notification.body || '')).replace(/\n/g, '<br>');
  var bubble =
    '<div class="chat-bubble eva-bubble eva-proactive">' +
    '<span class="eva">Eva:</span> ' +
    '<span class="eva-proactive-badge">Proactive</span> ' +
    '<strong>' + title + '</strong>' +
    '<div class="md">' + body + '</div></div>';
  txtOutput.innerHTML += bubble;
  txtOutput.scrollTop = txtOutput.scrollHeight;
}

async function pollNotifications() {
  if (_notifState.polling) return;
  _notifState.polling = true;
  try {
    var options = { method: 'GET' };
    if (typeof AbortSignal !== 'undefined' && AbortSignal.timeout) options.signal = AbortSignal.timeout(4000);
    var data = await backgroundBridgeRequest('/v1/notifications?unseen_only=1&limit=10', options);
    var items = data && Array.isArray(data.notifications) ? data.notifications : [];
    if (!items.length) return;
    var seenIds = [];
    var voiceText = [];
    items.forEach(function(notification) {
      injectProactiveBubble(notification);
      var channels = Array.isArray(notification.channels) ? notification.channels : ['chat'];
      if (channels.indexOf('voice') !== -1 && notification.body) {
        voiceText.push(String(notification.title || '') + '. ' + String(notification.body || ''));
      }
      if (notification.id) seenIds.push(notification.id);
    });
    if (voiceText.length && typeof speakText === 'function') {
      try { speakText(voiceText.join('. ')); } catch (_) {}
    }
    if (seenIds.length) {
      try {
        await backgroundBridgeRequest('/v1/notifications/seen', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ids: seenIds })
        });
      } catch (_) {}
    }
  } catch (_) {
    // Bridge unreachable or notifications unavailable; retry on the next tick.
  } finally {
    _notifState.polling = false;
  }
}

function initNotifications() {
  if (_notifState.timer) return;
  setTimeout(pollNotifications, 8000);
  _notifState.timer = setInterval(pollNotifications, _notifState.intervalMs);
}