// One fail-closed parser for every model-emitted Eva control block.
(function(root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.EvaAgentMarkers = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function() {
  'use strict';

  var KINDS = Object.freeze({
    browser: 'BROWSER', desktop: 'DESKTOP', camera: 'LOOK',
    signal: 'SIGNAL', action: 'ACTION'
  });
  var TOKEN_RE = /\[\[(\/?)EVA_(BROWSER|DESKTOP|LOOK|SIGNAL|ACTION)\]\]/g;

  function markRange(mask, start, end) {
    for (var index = start; index < end; index++) mask[index] = true;
  }

  function maskRenderedCode(source) {
    var mask = new Array(source.length).fill(false);
    function markMatches(expression) {
      expression.lastIndex = 0;
      var match;
      while ((match = expression.exec(source)) !== null) {
        markRange(mask, match.index, match.index + match[0].length);
        if (!match[0].length) expression.lastIndex += 1;
      }
    }
    // These exactly mirror renderMarkdown(): closed BBCode blocks, closed
    // triple-backtick fences, and paired single-line inline backticks.
    markMatches(/\[code(?:\s+lang=[\w.+-]+)?\]\s*[\s\S]*?\s*\[\/code\]/gi);
    markMatches(/```(?:[\w.+-]+)?\n[\s\S]*?```/g);
    var probe = source.split('').map(function(character, index) {
      return mask[index] ? (character === '\n' ? '\n' : ' ') : character;
    }).join('');
    var inline = /`[^`\n]+`/g;
    var inlineMatch;
    while ((inlineMatch = inline.exec(probe)) !== null) {
      markRange(mask, inlineMatch.index, inlineMatch.index + inlineMatch[0].length);
    }
    return source.split('').map(function(character, index) {
      return mask[index] ? (character === '\n' ? '\n' : ' ') : character;
    }).join('');
  }

  function strictJsonObject(source, maxLength) {
    if (typeof source !== 'string' || source.length > maxLength) {
      throw new Error('control JSON size is invalid');
    }
    var index = 0;
    function whitespace() { while (/\s/.test(source.charAt(index))) index += 1; }
    function stringValue() {
      if (source.charAt(index) !== '"') throw new Error('JSON string expected');
      var start = index++;
      while (index < source.length) {
        var character = source.charAt(index++);
        if (character === '"') {
          return JSON.parse(source.slice(start, index));
        }
        if (character === '\\') {
          if (index >= source.length) throw new Error('invalid JSON escape');
          var escape = source.charAt(index++);
          if (escape === 'u') {
            if (!/^[0-9a-fA-F]{4}$/.test(source.slice(index, index + 4))) {
              throw new Error('invalid JSON Unicode escape');
            }
            index += 4;
          } else if ('"\\/bfnrt'.indexOf(escape) === -1) {
            throw new Error('invalid JSON escape');
          }
        } else if (character.charCodeAt(0) < 0x20) {
          throw new Error('invalid JSON control character');
        }
      }
      throw new Error('unclosed JSON string');
    }
    function value(depth) {
      if (depth > 32) throw new Error('JSON nesting is too deep');
      whitespace();
      var character = source.charAt(index);
      if (character === '{') {
        index += 1;
        var object = Object.create(null);
        var keys = Object.create(null);
        whitespace();
        if (source.charAt(index) === '}') { index += 1; return object; }
        while (index < source.length) {
          whitespace();
          var key = stringValue();
          if (key === '__proto__' || key === 'prototype' || key === 'constructor') {
            throw new Error('forbidden JSON member');
          }
          if (Object.prototype.hasOwnProperty.call(keys, key)) {
            throw new Error('duplicate JSON member');
          }
          keys[key] = true;
          whitespace();
          if (source.charAt(index++) !== ':') throw new Error('JSON colon expected');
          object[key] = value(depth + 1);
          whitespace();
          character = source.charAt(index++);
          if (character === '}') return object;
          if (character !== ',') throw new Error('JSON comma expected');
        }
        throw new Error('unclosed JSON object');
      }
      if (character === '[') {
        index += 1;
        var array = [];
        whitespace();
        if (source.charAt(index) === ']') { index += 1; return array; }
        while (index < source.length) {
          array.push(value(depth + 1));
          whitespace();
          character = source.charAt(index++);
          if (character === ']') return array;
          if (character !== ',') throw new Error('JSON comma expected');
        }
        throw new Error('unclosed JSON array');
      }
      if (character === '"') return stringValue();
      var tail = source.slice(index);
      var literal = /^(true|false|null)/.exec(tail);
      if (literal) {
        index += literal[0].length;
        return literal[0] === 'true' ? true : literal[0] === 'false' ? false : null;
      }
      var number = /^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/.exec(tail);
      if (number) {
        index += number[0].length;
        var numeric = Number(number[0]);
        if (!Number.isFinite(numeric)) throw new Error('JSON number is invalid');
        return numeric;
      }
      throw new Error('JSON value is invalid');
    }
    var parsed = value(0);
    whitespace();
    if (index !== source.length || !parsed || typeof parsed !== 'object' ||
        Array.isArray(parsed)) {
      throw new Error('control JSON must be one object');
    }
    return parsed;
  }

  function minimalPayloadValid(kind, payload) {
    if (kind === 'BROWSER' || kind === 'DESKTOP') {
      return typeof payload.goal === 'string' && payload.goal.trim().length > 0;
    }
    if (kind === 'LOOK') {
      return Object.keys(payload).length === 1 &&
        typeof payload.question === 'string' &&
        payload.question.trim().length > 0 && payload.question.length <= 1000;
    }
    if (kind === 'SIGNAL') {
      return Object.keys(payload).length === 1 && typeof payload.message === 'string';
    }
    return kind === 'ACTION';
  }

  function normalizeText(value) {
    return String(value || '').replace(/\n{3,}/g, '\n\n').trim();
  }

  function parseResponse(value) {
    var source = String(value || '');
    if (source.length > 1024 * 1024) {
      return {
        text: source.slice(0, 16000), browser: null, desktop: null,
        camera: null, signal: false, actions: [], invalid: true,
        conflict: true, controlCount: 0
      };
    }
    var masked = maskRenderedCode(source);
    var tokens = [];
    TOKEN_RE.lastIndex = 0;
    var match;
    while ((match = TOKEN_RE.exec(masked)) !== null) {
      tokens.push({
        start: match.index, end: match.index + match[0].length,
        closing: match[1] === '/', kind: match[2]
      });
    }
    var prefixes = [];
    var candidateAt = 0;
    var candidateCount = 0;
    var candidateOverflow = false;
    while ((candidateAt = masked.indexOf('[[', candidateAt)) !== -1) {
      candidateCount += 1;
      if (candidateCount > 256) {
        candidateOverflow = true;
        break;
      }
      var candidateEnd = masked.indexOf(']]', candidateAt + 2);
      if (candidateEnd === -1) candidateEnd = masked.length;
      var candidate = masked.slice(candidateAt + 2, candidateEnd).normalize('NFKC');
      if (/e[^A-Za-z0-9]*v[^A-Za-z0-9]*a/i.test(candidate)) {
        prefixes.push(candidateAt);
      }
      // Advance from this opener rather than its closer so nested opener
      // floods count toward the same hard bound and fail closed.
      candidateAt += 2;
    }
    var exactStarts = Object.create(null);
    tokens.forEach(function(token) { exactStarts[token.start] = true; });
    var invalid = candidateOverflow || prefixes.some(function(position) {
      return !exactStarts[position];
    });
    var blocks = [];
    var active = null;
    tokens.forEach(function(token) {
      if (!token.closing) {
        if (active !== null) invalid = true;
        if (active === null) active = token;
        return;
      }
      if (active === null || active.kind !== token.kind) {
        invalid = true;
        return;
      }
      blocks.push({
        kind: active.kind, start: active.start, end: token.end,
        bodyStart: active.end, bodyEnd: token.start
      });
      active = null;
    });
    if (active !== null || blocks.length * 2 !== tokens.length) invalid = true;
    if (blocks.length > 1) invalid = true;

    var parsed = null;
    if (!invalid && blocks.length === 1) {
      var block = blocks[0];
      var lineStart = source.lastIndexOf('\n', block.start - 1) + 1;
      var lineEnd = source.indexOf('\n', block.end);
      if (lineEnd === -1) lineEnd = source.length;
      if (!/^[ \t]*$/.test(source.slice(lineStart, block.start)) ||
          !/^[ \t]*$/.test(source.slice(block.end, lineEnd))) {
        invalid = true;
      } else {
        try {
          parsed = strictJsonObject(
            source.slice(block.bodyStart, block.bodyEnd).trim(),
            block.kind === 'ACTION' ? 32 * 1024 : 8192
          );
          if (!minimalPayloadValid(block.kind, parsed)) invalid = true;
        } catch (_) {
          invalid = true;
        }
      }
    }

    var firstControl = prefixes.length ? Math.min.apply(Math, prefixes) : -1;
    var text = source;
    if (invalid && firstControl >= 0) {
      text = source.slice(0, firstControl);
    } else if (blocks.length === 1) {
      text = source.slice(0, blocks[0].start) + source.slice(blocks[0].end);
    }
    var result = {
      text: normalizeText(text), browser: null, desktop: null, camera: null,
      signal: false, actions: [], invalid: invalid, conflict: invalid,
      controlCount: invalid ? 0 : blocks.length
    };
    if (!invalid && blocks.length === 1) {
      if (blocks[0].kind === 'BROWSER') result.browser = parsed;
      else if (blocks[0].kind === 'DESKTOP') result.desktop = parsed;
      else if (blocks[0].kind === 'LOOK') result.camera = parsed;
      else if (blocks[0].kind === 'SIGNAL') result.signal = true;
      else if (blocks[0].kind === 'ACTION') {
        result.actions.push({ payload: parsed, raw: source.slice(blocks[0].bodyStart, blocks[0].bodyEnd).trim() });
      }
    }
    return result;
  }

  function extract(text, kind) {
    if (!Object.prototype.hasOwnProperty.call(KINDS, kind)) {
      throw new Error('unsupported agent marker');
    }
    var result = parseResponse(text);
    var payload = kind === 'browser' ? result.browser
      : kind === 'desktop' ? result.desktop
      : kind === 'camera' ? result.camera : null;
    return {
      text: result.text, payload: payload,
      validCount: payload && !result.invalid ? 1 : 0
    };
  }

  function extractControlMarkers(text) {
    var result = parseResponse(text);
    return {
      text: result.text, browser: result.browser, desktop: result.desktop,
      camera: result.camera, signal: result.signal, actions: result.actions,
      invalid: result.invalid, conflict: result.conflict
    };
  }

  return Object.freeze({
    extract: extract, extractControlMarkers: extractControlMarkers,
    parseResponse: parseResponse, strictJsonObject: strictJsonObject
  });
});
