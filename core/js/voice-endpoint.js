(function (root) {
  'use strict';

  function normalize(text) {
    return String(text || '').replace(/\s+/g, ' ').trim();
  }

  function mergeTranscript(left, right) {
    left = normalize(left);
    right = normalize(right);
    if (!left) return right;
    if (!right) return left;

    var leftLower = left.toLowerCase();
    var rightLower = right.toLowerCase();
    if (leftLower === rightLower || leftLower.indexOf(rightLower) === 0) return left;
    if (rightLower.indexOf(leftLower) === 0) return right;

    var leftWords = left.split(' ');
    var rightWords = right.split(' ');
    var maxOverlap = Math.min(leftWords.length, rightWords.length);
    for (var count = maxOverlap; count > 0; count--) {
      var suffix = leftWords.slice(-count).join(' ').toLowerCase();
      var prefix = rightWords.slice(0, count).join(' ').toLowerCase();
      if (suffix === prefix) return leftWords.concat(rightWords.slice(count)).join(' ');
    }
    return left + ' ' + right;
  }

  function VoiceEndpoint(options) {
    options = options || {};
    this.delayMs = Number(options.delayMs) || 2200;
    this.onCommit = options.onCommit || function () {};
    this.onEvent = options.onEvent || function () {};
    this.setTimer = options.setTimer || setTimeout;
    this.clearTimer = options.clearTimer || clearTimeout;
    this.pending = '';
    this.fragments = 0;
    this.timer = null;
  }

  VoiceEndpoint.prototype.setDelay = function (delayMs) {
    var parsed = Number(delayMs);
    if (Number.isFinite(parsed)) this.delayMs = Math.max(1000, Math.min(parsed, 5000));
  };

  VoiceEndpoint.prototype.accept = function (text, metadata) {
    var fragment = normalize(text);
    if (!fragment) return false;
    metadata = metadata || {};

    var previous = this.pending;
    var merged = mergeTranscript(previous, fragment);
    if (previous && merged === previous) {
      var duplicateEvent = { type: 'duplicate', provider: metadata.provider || '', chars: fragment.length };
      this.onEvent(duplicateEvent);
      if (root.EvaLearning) root.EvaLearning.recordVoiceDiagnostic(duplicateEvent);
    } else {
      this.pending = merged;
      this.fragments += 1;
      var mergeEvent = {
        type: previous ? 'merged' : 'buffered',
        provider: metadata.provider || '',
        chars: this.pending.length,
        fragments: this.fragments
      };
      this.onEvent(mergeEvent);
      if (root.EvaLearning) root.EvaLearning.recordVoiceDiagnostic(mergeEvent);
    }

    if (this.timer) this.clearTimer(this.timer);
    var self = this;
    var requestedDelay = Number(metadata.delayMs);
    var delay = Number.isFinite(requestedDelay) ? Math.max(250, requestedDelay) : this.delayMs;
    this.timer = this.setTimer(function () { self.flush('silence'); }, delay);
    return true;
  };

  VoiceEndpoint.prototype.flush = function (reason) {
    if (this.timer) this.clearTimer(this.timer);
    this.timer = null;
    var text = this.pending;
    var fragments = this.fragments;
    this.pending = '';
    this.fragments = 0;
    if (!text) return false;
    var commitEvent = { type: 'committed', reason: reason || 'manual', chars: text.length, fragments: fragments };
    this.onEvent(commitEvent);
    if (root.EvaLearning) root.EvaLearning.recordVoiceDiagnostic(commitEvent);
    this.onCommit(text);
    return true;
  };

  VoiceEndpoint.prototype.reset = function () {
    if (this.timer) this.clearTimer(this.timer);
    this.timer = null;
    if (this.pending) {
      var interruptedEvent = { type: 'interrupted', chars: this.pending.length, fragments: this.fragments };
      this.onEvent(interruptedEvent);
      if (root.EvaLearning) root.EvaLearning.recordVoiceDiagnostic(interruptedEvent);
    }
    this.pending = '';
    this.fragments = 0;
  };

  VoiceEndpoint.mergeTranscript = mergeTranscript;
  root.VoiceEndpoint = VoiceEndpoint;
})(typeof window !== 'undefined' ? window : globalThis);