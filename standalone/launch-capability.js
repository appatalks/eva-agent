'use strict';

const crypto = require('crypto');
const FORBIDDEN_TEXT = /[\u0000\u0008\u000B\u000C\u000E-\u001F\u007F-\u009F\u061C\u200B-\u200F\u2028-\u202E\u2060\u2066-\u2069\uFEFF]/;

function normalize(value) {
  if (value === null || typeof value === 'boolean') return value;
  if (typeof value === 'string') {
    for (let index = 0; index < value.length; index += 1) {
      const unit = value.charCodeAt(index);
      if (unit >= 0xD800 && unit <= 0xDBFF) {
        const next = value.charCodeAt(index + 1);
        if (!(next >= 0xDC00 && next <= 0xDFFF)) {
          throw new Error('launch specification contains invalid Unicode');
        }
        index += 1;
      } else if (unit >= 0xDC00 && unit <= 0xDFFF) {
        throw new Error('launch specification contains invalid Unicode');
      }
    }
    const normalized = value.normalize('NFC');
    if (FORBIDDEN_TEXT.test(normalized)) {
      throw new Error('launch specification contains forbidden control text');
    }
    return normalized;
  }
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw new Error('launch specification contains non-finite data');
    return value;
  }
  if (Array.isArray(value)) return value.map(normalize);
  if (value && typeof value === 'object') {
    const output = {};
    Object.keys(value).sort().forEach(function(key) {
      output[String(key)] = normalize(value[key]);
    });
    return output;
  }
  throw new Error('launch specification contains unsupported data');
}

function exactKeys(value, allowed) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const keys = Object.keys(value).sort();
  const wanted = allowed.slice().sort();
  return keys.length === wanted.length && keys.every(function(key, index) {
    return key === wanted[index];
  });
}

function boundedString(value, field, limit, allowEmpty) {
  if (typeof value !== 'string') throw new Error(field + ' must be text');
  const normalized = normalize(value);
  if (!allowEmpty && !normalized.trim()) throw new Error(field + ' is required');
  if (Array.from(normalized).length > limit) throw new Error(field + ' is too long');
  return normalized;
}

function normalizePublicOrigin(value) {
  const text = boundedString(value, 'postcondition.origin', 2048, false);
  const match = /^(https?):\/\/([A-Za-z0-9.-]+)(?::([0-9]{1,5}))?\/?$/i.exec(text);
  if (!match) {
    throw new Error('postcondition.origin must contain only an HTTP(S) origin');
  }
  const scheme = match[1].toLowerCase();
  const hostname = match[2].toLowerCase().replace(/\.$/, '');
  const labels = hostname.split('.');
  if (hostname.length > 253 || !hostname.includes('.') ||
      hostname === 'localhost' || hostname.endsWith('.localhost') ||
      hostname.endsWith('.local') || /^[0-9.]+$/.test(hostname) ||
      labels.some(function(label) {
        return !label || label.length > 63 ||
          !/^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/.test(label);
      })) {
    throw new Error('postcondition.origin host is invalid');
  }
  const numericPort = match[3] === undefined ? null : Number(match[3]);
  if (numericPort !== null && (numericPort < 1 || numericPort > 65535)) {
    throw new Error('postcondition.origin port is invalid');
  }
  const defaultPort = scheme === 'https' ? 443 : 80;
  const port = numericPort !== null && numericPort !== defaultPort
    ? ':' + numericPort : '';
  return scheme + '://' + hostname + port;
}

function validateStartUrl(value) {
  if (!value) return value;
  let parsed;
  try { parsed = new URL(value); } catch (_) { throw new Error('start_url is invalid'); }
  if ((parsed.protocol !== 'http:' && parsed.protocol !== 'https:') ||
      parsed.username || parsed.password || !parsed.hostname) {
    throw new Error('start_url must use public HTTP(S) without credentials');
  }
  const authorityMatch = /^[A-Za-z][A-Za-z0-9+.-]*:\/\/([^/?#]*)/.exec(value);
  if (!authorityMatch || !/^[\x00-\x7F]+$/.test(authorityMatch[1])) {
    throw new Error('start_url host must be ASCII');
  }
  const rawAuthority = /^([A-Za-z0-9.-]+)(?::([0-9]{1,5}))?$/.exec(authorityMatch[1]);
  if (!rawAuthority) throw new Error('start_url host is invalid');
  if (rawAuthority[2] !== undefined) {
    const port = Number(rawAuthority[2]);
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      throw new Error('start_url port is invalid');
    }
  }
  const hostname = rawAuthority[1].toLowerCase().replace(/\.$/, '');
  const labels = hostname.split('.');
  if (hostname === 'localhost' || hostname.endsWith('.localhost') ||
      hostname.endsWith('.local') || !hostname.includes('.') ||
      /^[0-9a-fx:.]+$/i.test(hostname) ||
      labels.some(function(label) {
        return !label || label.length > 63 ||
          !/^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/.test(label);
      })) {
    throw new Error('start_url host is invalid');
  }
  return value;
}

function validatePostcondition(agent, value) {
  if (value === null || value === undefined) return null;
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('postcondition must be an object');
  }
  const type = value.type;
  if (agent === 'browser' && type === 'browser.url_match') {
    if (!exactKeys(value, ['type', 'origin', 'path'])) throw new Error('invalid URL postcondition');
    return {
      type: type,
      origin: normalizePublicOrigin(value.origin),
      path: (function() {
        const path = boundedString(value.path, 'postcondition.path', 512, false);
        if (!path.startsWith('/') || path.includes('?') || path.includes('#')) {
          throw new Error('postcondition.path must be an absolute path');
        }
        return path;
      })()
    };
  }
  if (agent === 'browser' && type === 'browser.element_state') {
    const state = value.state;
    const fields = state === 'count_equals'
      ? ['type', 'selector', 'state', 'count']
      : state === 'text_hash_equals'
        ? ['type', 'selector', 'state', 'text_hash']
        : ['type', 'selector', 'state'];
    if (!['visible', 'hidden', 'count_equals', 'text_hash_equals'].includes(state) ||
        !exactKeys(value, fields)) throw new Error('invalid element postcondition');
    const output = {
      type: type,
      selector: boundedString(value.selector, 'postcondition.selector', 256, false),
      state: state
    };
    if (state === 'count_equals') {
      if (!Number.isInteger(value.count) || value.count < 0 || value.count > 1000) {
        throw new Error('postcondition.count is invalid');
      }
      output.count = value.count;
    }
    if (state === 'text_hash_equals') {
      if (typeof value.text_hash !== 'string' || !/^[0-9a-f]{64}$/.test(value.text_hash)) {
        throw new Error('postcondition.text_hash is invalid');
      }
      output.text_hash = value.text_hash;
    }
    return output;
  }
  if (agent === 'desktop' && type === 'desktop.process_spawned') {
    if (!exactKeys(value, ['type', 'executable', 'state']) || value.state !== 'started') {
      throw new Error('invalid process postcondition');
    }
    const executable = boundedString(
      value.executable, 'postcondition.executable', 64, false
    );
    if (!/^[A-Za-z0-9._+-]{1,64}$/.test(executable)) {
      throw new Error('postcondition.executable is invalid');
    }
    return { type: type, executable: executable.toLowerCase(), state: 'started' };
  }
  throw new Error('unsupported postcondition type');
}

function buildSpec(agent, data, runtimeDefaults) {
  if (agent !== 'browser' && agent !== 'desktop' && agent !== 'camera') {
    throw new Error('invalid agent');
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('invalid launch data');
  if (agent === 'camera') {
    if (!exactKeys(data, ['question', 'device'])) {
      throw new Error('invalid camera specification');
    }
    const device = data.device;
    if (!Number.isInteger(device) || device < 0 || device > 32) {
      throw new Error('camera device is invalid');
    }
    return normalize({
      question: boundedString(data.question, 'question', 1000, false),
      device: device
    });
  }
  const goal = boundedString(data.goal, 'goal', 2000, false);
  const defaults = runtimeDefaults && typeof runtimeDefaults === 'object'
    ? runtimeDefaults : {};
  const defaultVisionModel = defaults.vision_model ||
    process.env.OPENAI_VISION_MODEL || 'gpt-4o';
  const rawVisionModel = data.vision_model === undefined || data.vision_model === ''
    ? defaultVisionModel : data.vision_model;
  const visionModel = boundedString(
    rawVisionModel,
    'vision_model', 128, true
  );
  const useDirector = data.use_director === undefined ? true : data.use_director;
  if (typeof useDirector !== 'boolean') throw new Error('use_director must be boolean');
  const autonomy = data.autonomy === undefined ? 'pause' : data.autonomy;
  if (autonomy !== 'pause' && autonomy !== 'confirm_all') {
    throw new Error('autonomy must be pause or confirm_all');
  }
  const maxSteps = data.max_steps === undefined ? 25 : data.max_steps;
  if (!Number.isInteger(maxSteps) || maxSteps < 1 || maxSteps > 60) {
    throw new Error('max_steps must be an integer between 1 and 60');
  }
  const spec = {
    goal: goal,
    vision_model: visionModel,
    use_director: useDirector,
    autonomy: autonomy,
    max_steps: maxSteps,
    postcondition: validatePostcondition(agent, data.postcondition)
  };
  if (agent === 'browser') {
    spec.start_url = boundedString(
      data.start_url === undefined ? '' : data.start_url,
      'start_url', 2048, true
    );
    if (spec.start_url && spec.start_url.trim() !== spec.start_url) {
      throw new Error('start_url must not contain surrounding whitespace');
    }
    spec.start_url = validateStartUrl(spec.start_url);
    spec.headless = data.headless === undefined ? false : data.headless;
    if (typeof spec.headless !== 'boolean') throw new Error('headless must be boolean');
  }
  return normalize(spec);
}

function canonicalSpec(agent, data) {
  return JSON.stringify(buildSpec(agent, data));
}

function specHash(agent, data) {
  return crypto.createHash('sha256').update(canonicalSpec(agent, data), 'utf8').digest('hex');
}

function displaySummary(agent, spec) {
  const canonical = canonicalSpec(agent, spec);
  return [
    'Agent authority: ' + (
      agent === 'browser' ? 'browser' :
      agent === 'camera' ? 'one-camera-frame' : 'desktop-launch-only'
    ),
    'Exact signed launch specification (canonical JSON):',
    canonical,
    '',
    agent === 'camera'
      ? 'This one-use capability permits one fresh webcam frame for the exact question shown.'
      : spec.postcondition
      ? 'Success requires the signed condition to change from not observed to observed after an approved effect.'
      : 'No success condition is signed; model completion will remain unverified.',
    'This capability permits only launch admission. Every effectful action requires a separate approval.'
  ].join('\n');
}

function issue(secret, agent, data, nowSeconds) {
  if (typeof secret !== 'string' || !secret) throw new Error('missing launch authority');
  const now = Number.isInteger(nowSeconds) ? nowSeconds : Math.floor(Date.now() / 1000);
  const payload = {
    version: 1,
    agent: agent,
    spec_hash: specHash(agent, data),
    nonce: crypto.randomBytes(16).toString('hex'),
    expires_at: now + 60
  };
  const encoded = Buffer.from(JSON.stringify(payload), 'utf8').toString('base64url');
  const signature = crypto.createHmac('sha256', secret).update(encoded, 'utf8').digest('base64url');
  return encoded + '.' + signature;
}

module.exports = { buildSpec, canonicalSpec, specHash, displaySummary, issue };
