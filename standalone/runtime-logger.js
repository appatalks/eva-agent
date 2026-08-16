'use strict';

const fs = require('fs');
const path = require('path');

const DEFAULT_MAX_BYTES = 10 * 1024 * 1024;
const DEFAULT_BACKUPS = 3;
const DEFAULT_LINE_LIMIT = 4096;

const SECRET_KEY_FRAGMENT = '(?:(?:api|private)[_-]?key|authorization|cookie|credential|password|secret|token)';
const SECRET_ASSIGNMENT_RE = new RegExp(
  '(^|[^A-Za-z0-9])([A-Za-z0-9_.-]*' + SECRET_KEY_FRAGMENT + '[A-Za-z0-9_.-]*)\\s*[:=]\\s*([^\\s,;]+)',
  'gi'
);
const AUTHORIZATION_HEADER_RE = /\bAuthorization\s*[:=]\s*[^\r\n]*/gi;
const PRIVATE_KEY_BLOCK_RE = /-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z0-9]+ )?PRIVATE KEY-----/g;
const BEARER_RE = /\bBearer\s+[^\s,;]+/gi;
const PROVIDER_TOKEN_RE = /\b(?:sk-[A-Za-z0-9_-]{12,}|AIza[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{12,}|github_pat_[A-Za-z0-9_]{12,}|xox[baprs]-[A-Za-z0-9-]{12,})\b/g;
const URL_SECRET_RE = /([?&](?:[A-Za-z0-9_.-]*(?:(?:api|private)[_-]?key|token|secret|password|credential|signature|sig)[A-Za-z0-9_.-]*|code|device[_-]?code|user[_-]?code)=)[^&#\s]+/gi;
const JSON_SECRET_RE = new RegExp(
  "([\"'][A-Za-z0-9_.-]*" + SECRET_KEY_FRAGMENT + "[A-Za-z0-9_.-]*[\"']\\s*:\\s*)[\"'][^\"']*[\"']",
  'gi'
);
const PRIVATE_FIELD_NAME = '(?:[A-Za-z0-9._-]+[_-])?(?:body|prompt|messages?|response|input|content|transcript|text)';
const BODY_FIELD_RE = new RegExp(
  String.raw`(["']${PRIVATE_FIELD_NAME}["']\s*:\s*)["'][^"']*["']`,
  'gi'
);
const ESCAPED_BODY_FIELD_RE = new RegExp(
  String.raw`(\\"${PRIVATE_FIELD_NAME}\\"\s*:\s*)\\".*?\\"`,
  'gi'
);
const MCP_DIAGNOSTIC_RE = /(\[MCP:[^\]]+\])\s+.*/g;
const CONTENT_DIAGNOSTIC_RES = [
  /(Auto-(?:reflection|summary)(?:\s+#\d+)?:)\s+.*/gi,
  /(JSON parse failed, first \d+ chars:)\s+.*/gi,
  /(Kusto (?:query HTTP \d+|ingest error in response|ingest failed \(\d+\)):)\s+.*/gi,
  /(Embedding API failed \(\d+\):)\s+.*/gi
];

const SENSITIVE_JSON_KEY_RE = new RegExp(SECRET_KEY_FRAGMENT, 'i');
const PRIVATE_JSON_KEY_RE = new RegExp('^' + PRIVATE_FIELD_NAME + '$', 'i');

function sanitizeJsonValue(value, key, parentKey) {
  const field = String(key || '');
  if (SENSITIVE_JSON_KEY_RE.test(field)) return '<redacted>';
  if (PRIVATE_JSON_KEY_RE.test(field)) return '<content omitted>';
  if (field === 'content') {
    return '<content omitted>';
  }
  if (field === 'text' && parentKey === 'content') return '<content omitted>';
  if ((field === 'message' || field === 'transcript') && typeof value === 'string' && value.length > 80) {
    return '<content omitted>';
  }
  if (Array.isArray(value)) return value.map((item) => sanitizeJsonValue(item, '', field || parentKey));
  if (value && typeof value === 'object') {
    const safe = {};
    Object.keys(value).slice(0, 128).forEach((childKey) => {
      safe[childKey] = sanitizeJsonValue(value[childKey], childKey, field || parentKey);
    });
    return safe;
  }
  return value;
}

function balancedJsonEnd(text, start) {
  const opening = text[start];
  if (opening !== '{' && opening !== '[') return -1;
  const stack = [opening];
  let inString = false;
  let escaped = false;
  for (let index = start + 1; index < text.length; index += 1) {
    const character = text[index];
    if (inString) {
      if (escaped) escaped = false;
      else if (character === '\\') escaped = true;
      else if (character === '"') inString = false;
      continue;
    }
    if (character === '"') {
      inString = true;
    } else if (character === '{' || character === '[') {
      stack.push(character);
    } else if (character === '}' || character === ']') {
      const expected = character === '}' ? '{' : '[';
      if (stack.pop() !== expected) return -1;
      if (!stack.length) return index + 1;
    }
  }
  return -1;
}

function balancedEscapedJsonEnd(text, start) {
  const opening = text[start];
  if (opening !== '{' && opening !== '[') return -1;
  const stack = [opening];
  let inString = false;
  for (let index = start + 1; index < text.length; index += 1) {
    const character = text[index];
    if (character === '"') {
      let slashes = 0;
      for (let scan = index - 1; scan >= start && text[scan] === '\\'; scan -= 1) slashes += 1;
      if (slashes % 2 === 1 && Math.floor(slashes / 2) % 2 === 0) inString = !inString;
      continue;
    }
    if (inString) continue;
    if (character === '{' || character === '[') {
      stack.push(character);
    } else if (character === '}' || character === ']') {
      const expected = character === '}' ? '{' : '[';
      if (stack.pop() !== expected) return -1;
      if (!stack.length) return index + 1;
    }
  }
  return -1;
}

function sanitizeEscapedJsonFragment(fragment) {
  try {
    const decoded = JSON.parse('"' + fragment + '"');
    const parsed = JSON.parse(decoded);
    return JSON.stringify(sanitizeJsonValue(parsed, '', ''));
  } catch (_) {
    return null;
  }
}

function redactStructuredJson(text) {
  let output = '';
  let cursor = 0;
  while (cursor < text.length) {
    const objectStart = text.indexOf('{', cursor);
    const arrayStart = text.indexOf('[', cursor);
    const candidates = [objectStart, arrayStart].filter((index) => index >= 0);
    if (!candidates.length) return output + text.slice(cursor);
    const start = Math.min.apply(Math, candidates);
    let end = balancedJsonEnd(text, start);
    let escaped = false;
    if (end < 0) {
      end = balancedEscapedJsonEnd(text, start);
      escaped = end >= 0;
    }
    if (end < 0) {
      output += text.slice(cursor, start + 1);
      cursor = start + 1;
      continue;
    }
    const fragment = text.slice(start, end);
    try {
      const sanitized = escaped
        ? sanitizeEscapedJsonFragment(fragment)
        : JSON.stringify(sanitizeJsonValue(JSON.parse(fragment), '', ''));
      if (sanitized === null) throw new Error('Invalid escaped JSON');
      output += text.slice(cursor, start) + sanitized;
      cursor = end;
    } catch (_) {
      output += text.slice(cursor, start + 1);
      cursor = start + 1;
    }
  }
  return output;
}

function redactRuntimeText(value, limit) {
  let text = String(value == null ? '' : value);
  text = text.replace(PRIVATE_KEY_BLOCK_RE, '<redacted-private-key>');
  text = text.replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '');
  text = redactStructuredJson(text);
  text = text.replace(BEARER_RE, 'Bearer <redacted>');
  text = text.replace(SECRET_ASSIGNMENT_RE, '$1$2=<redacted>');
  text = text.replace(PROVIDER_TOKEN_RE, '<redacted>');
  text = text.replace(URL_SECRET_RE, '$1<redacted>');
  text = text.replace(JSON_SECRET_RE, '$1"<redacted>"');
  text = text.replace(BODY_FIELD_RE, '$1"<content omitted>"');
  text = text.replace(ESCAPED_BODY_FIELD_RE, '$1\\"<content omitted>\\"');
  text = text.replace(MCP_DIAGNOSTIC_RE, '$1 <server diagnostic omitted; see bridge_debug.log>');
  CONTENT_DIAGNOSTIC_RES.forEach((pattern) => {
    text = text.replace(pattern, '$1 <content omitted>');
  });
  text = text.replace(AUTHORIZATION_HEADER_RE, 'Authorization: <redacted>');
  text = text.replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, '');
  const maximum = Math.max(256, Number(limit) || DEFAULT_LINE_LIMIT);
  return text.length > maximum ? text.slice(0, maximum) + '…' : text;
}

class RuntimeLogger {
  constructor(options) {
    const config = options || {};
    if (!config.logPath) throw new Error('Runtime log path is required.');
    this.logPath = path.resolve(config.logPath);
    this.maxBytes = Math.max(1024, Number(config.maxBytes) || DEFAULT_MAX_BYTES);
    this.backups = Math.max(1, Number(config.backups) || DEFAULT_BACKUPS);
    this.lineLimit = Math.max(256, Number(config.lineLimit) || DEFAULT_LINE_LIMIT);
    this.fd = null;
    this.writing = false;
    this.streamsInstalled = false;
    this.originalStdoutWrite = null;
    this.originalStderrWrite = null;
    this.buffers = { stdout: '', stderr: '' };
    this.open();
  }

  rotateIfNeeded(extraBytes) {
    let size = 0;
    try { size = fs.statSync(this.logPath).size; } catch (_) {}
    if (size + (extraBytes || 0) <= this.maxBytes) return;
    this.closeFile();
    for (let index = this.backups; index >= 1; index -= 1) {
      const source = index === 1 ? this.logPath : this.logPath + '.' + (index - 1);
      const target = this.logPath + '.' + index;
      try { fs.rmSync(target, { force: true }); } catch (_) {}
      try { fs.renameSync(source, target); } catch (_) {}
    }
    this.openFile();
  }

  openFile() {
    try {
      fs.mkdirSync(path.dirname(this.logPath), { recursive: true, mode: 0o700 });
      this.fd = fs.openSync(this.logPath, 'a', 0o600);
      try { fs.chmodSync(this.logPath, 0o600); } catch (_) {}
    } catch (_) {
      this.fd = null;
    }
  }

  open() {
    this.rotateIfNeeded(0);
    if (this.fd === null) this.openFile();
    this.event('runtime', 'session_start', { pid: process.pid, platform: process.platform });
  }

  closeFile() {
    if (this.fd === null) return;
    try { fs.closeSync(this.fd); } catch (_) {}
    this.fd = null;
  }

  write(source, level, message) {
    if (this.writing) return;
    this.writing = true;
    try {
      const safeSource = redactRuntimeText(source || 'runtime', 80).replace(/\s+/g, '_');
      const safeLevel = redactRuntimeText(level || 'info', 24).toUpperCase();
      const safeMessage = redactRuntimeText(message, this.lineLimit).replace(/[\r\n]+/g, ' ').trim();
      if (!safeMessage) return;
      const line = new Date().toISOString() + ' [' + safeLevel + '] [' + safeSource + '] ' + safeMessage + '\n';
      this.rotateIfNeeded(Buffer.byteLength(line));
      if (this.fd !== null) fs.writeSync(this.fd, line, null, 'utf8');
    } catch (_) {
      // Logging must never interrupt Eva.
    } finally {
      this.writing = false;
    }
  }

  event(source, name, fields) {
    const safeFields = {};
    Object.keys(fields || {}).slice(0, 32).forEach((key) => {
      const value = fields[key];
      safeFields[String(key).slice(0, 80)] = typeof value === 'string'
        ? redactRuntimeText(value, 500)
        : value;
    });
    this.write(source, 'event', name + (Object.keys(safeFields).length ? ' ' + JSON.stringify(safeFields) : ''));
  }

  consumeStream(source, level, chunk) {
    const key = source === 'electron-stderr' ? 'stderr' : 'stdout';
    let buffer = this.buffers[key] + String(chunk == null ? '' : chunk);
    const parts = buffer.split(/[\r\n]+/);
    this.buffers[key] = parts.pop() || '';
    parts.forEach((line) => this.write(source, level, line));
    if (this.buffers[key].length > this.lineLimit * 2) {
      this.write(source, level, this.buffers[key]);
      this.buffers[key] = '';
    }
  }

  installProcessStreams() {
    if (this.streamsInstalled) return;
    this.streamsInstalled = true;
    this.originalStdoutWrite = process.stdout.write;
    this.originalStderrWrite = process.stderr.write;
    const logger = this;
    process.stdout.write = function(chunk, encoding, callback) {
      logger.consumeStream('electron-stdout', 'info', chunk);
      return logger.originalStdoutWrite.call(process.stdout, chunk, encoding, callback);
    };
    process.stderr.write = function(chunk, encoding, callback) {
      logger.consumeStream('electron-stderr', 'error', chunk);
      return logger.originalStderrWrite.call(process.stderr, chunk, encoding, callback);
    };
  }

  attachRenderer(webContents) {
    if (!webContents || typeof webContents.on !== 'function') return;
    const logger = this;
    webContents.on('console-message', function(event, detailsOrLevel, message, lineNumber, sourceId) {
      const details = detailsOrLevel && typeof detailsOrLevel === 'object'
        ? detailsOrLevel
        : { level: detailsOrLevel, message: message, lineNumber: lineNumber, sourceId: sourceId };
      const levelMap = { 0: 'debug', 1: 'info', 2: 'warning', 3: 'error' };
      const level = levelMap[details.level] || String(details.level || 'info');
      const location = details.sourceId ? ' ' + path.basename(String(details.sourceId)) + ':' + (details.lineNumber || 0) : '';
      logger.write('renderer', level, String(details.message || '') + location);
    });
    webContents.on('render-process-gone', function(_event, details) {
      logger.event('renderer', 'process_gone', {
        reason: details && details.reason,
        exitCode: details && details.exitCode
      });
    });
  }

  close() {
    Object.keys(this.buffers).forEach((key) => {
      if (this.buffers[key]) this.write('electron-' + key, key === 'stderr' ? 'error' : 'info', this.buffers[key]);
      this.buffers[key] = '';
    });
    this.event('runtime', 'session_end', { pid: process.pid });
    if (this.streamsInstalled) {
      if (this.originalStdoutWrite) process.stdout.write = this.originalStdoutWrite;
      if (this.originalStderrWrite) process.stderr.write = this.originalStderrWrite;
      this.streamsInstalled = false;
    }
    this.closeFile();
  }
}

module.exports = {
  RuntimeLogger,
  redactRuntimeText,
  DEFAULT_MAX_BYTES,
  DEFAULT_BACKUPS
};
