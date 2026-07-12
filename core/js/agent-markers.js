// Strict parser for model-requested browser/desktop launch markers.
// Closing delimiters are mandatory so nested JSON (postconditions) is never
// truncated by a non-balanced regular expression.
(function(root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.EvaAgentMarkers = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function() {
  'use strict';

  var KINDS = { browser: 'EVA_BROWSER', desktop: 'EVA_DESKTOP' };

  function extract(text, kind) {
    var marker = KINDS[kind];
    if (!marker) throw new Error('unsupported agent marker');
    var payload = null;
    var validCount = 0;
    var source = String(text || '');
    var expression = new RegExp(
      '\\[\\[' + marker + '\\]\\]\\s*([\\s\\S]*?)\\s*\\[\\[/' + marker + '\\]\\]',
      'g'
    );
    var output = source.replace(expression, function(full, raw) {
      var parsed = null;
      if (raw.length <= 8192) {
        try { parsed = JSON.parse(raw); } catch (_) { parsed = null; }
      }
      var valid = parsed && !Array.isArray(parsed) &&
        typeof parsed.goal === 'string' && parsed.goal.trim().length > 0;
      if (valid) validCount += 1;
      if (valid && payload === null) payload = parsed;
      return valid ? ('\n_Opening the ' + kind + ' agent…_\n') : '';
    });
    return { text: output, payload: payload, validCount: validCount };
  }

  function extractControlMarkers(text) {
    var browser = extract(text, 'browser');
    var desktop = extract(browser.text, 'desktop');
    if (browser.validCount + desktop.validCount > 1) {
      return {
        text: desktop.text
          .replace(/\n_Opening the (?:browser|desktop) agent…_\n/g, '') +
          '\n\n_Agent launch rejected: one response requested multiple control surfaces._',
        browser: null,
        desktop: null,
        conflict: true
      };
    }
    return {
      text: desktop.text,
      browser: browser.payload,
      desktop: desktop.payload,
      conflict: false
    };
  }

  return Object.freeze({ extract: extract, extractControlMarkers: extractControlMarkers });
});
