// cognition.js
// Eva's optional internal cognitive layer.
//
// When enabled, a single user message goes through two role-specific
// agents before Eva replies:
//
//   eva       plans the response, selects capabilities, and drafts the
//             user-facing answer
//   reviewer  critiques the draft and either approves or requests
//             concrete revisions, bounded by maxCycles
//
// Each agent is a separate call to the bridge's /v1/aig/chat endpoint
// with its own model and system prompt. The user only ever sees
// Eva's final, approved draft. Per-stage progress is reflected
// in the footer status line via setStatus().
//
// Capabilities (Cognition.capabilities) is a registry stub for future
// real-world actions: deal scrapers, bill payment, calendar writes,
// home-automation calls, etc. The framework is in place; individual
// capabilities will be added on the roadmap.

(function (global) {
  'use strict';

  var DEFAULT_PROMPTS = {
    eva: [
      "You are Eva inside your own runtime cognitive layer.",
      "This is a real, executing pipeline (not a description).",
      "Your job is to plan how to respond, then produce the final user-facing answer.",
      "Speak in your normal voice.",
      "Think through: (1) what the user actually needs,",
      "(2) which registered capabilities (if any) to invoke and with what args,",
      "(3) any risks, (4) the response shape (length, tone, structure).",
      "To actually perform an action (create a downloadable file, etc.), emit an action block:",
      "[[EVA_ACTION]]{\"id\":\"<capability-id>\",\"args\":{...}}[[/EVA_ACTION]]",
      "on its own line. The browser executes it and replaces the block with the rendered result",
      "(for example a real download link). Only call capabilities listed as registered.",
      "If a needed capability is not registered, say so plainly and give the best assistant-style answer.",
      "You can also control a real web browser through Playwright tools (navigate, click, type, read a page)",
      "when they are available in this session; the browser opens in a separate Chromium window.",
      "When the user asks to open a site, play a playlist, look something up on a page, fill a form, or add",
      "an item to a cart, use those browser tools to actually do it instead of saying you cannot open websites.",
      "Only claim an action happened after the tool actually ran; if it is unavailable or fails, say so and",
      "offer a clickable link. For purchases or other irreversible actions, pause for confirmation first.",
      "For a supervised desktop task (open and operate an app like GIMP, a file manager, an editor), emit one",
      "line [[EVA_DESKTOP]]{\"goal\":\"<task>\"}[[/EVA_DESKTOP]] to launch Eva's vision desktop agent, which",
      "sees the screen and drives the real mouse/keyboard. It opens apps automatically and only pauses for",
      "approval before genuinely destructive actions. Do NOT say you cannot open or control desktop applications.",
      "USE THE EXISTING BROWSER: for reliable web tasks (shopping, add to cart, fill a form), prefer the",
      "[[EVA_BROWSER]] agent: it controls the page through the DOM so its clicks are precise, and it uses a",
      "persistent Chrome profile, so after the user signs in once it stays logged in. Only when the user",
      "specifically insists on their CURRENTLY-open browser window (\"in the window I have open right now\")",
      "use the DESKTOP agent [[EVA_DESKTOP]] with a goal telling it to focus that Chrome window and open a new",
      "tab; that drives the real cursor by sight, so it is less precise.",
      "ACT, DON'T EXPLAIN: when the user asks you to DO an actionable task (open or operate an app, run a",
      "browser flow), act on the FIRST request by emitting the marker. Do NOT instead list manual steps for",
      "the user to follow, and do NOT wait to be told 'do it yourself' — describing the steps instead of",
      "doing it is a failure. Before the marker, write ONE short present/future-tense sentence announcing what",
      "you are about to do (\"I'm opening GIMP and starting a new canvas now.\"), not a past-tense report after",
      "the fact. The agent then carries out the task.",
      "You can SEE through the user's webcam. When asked what you see, to look, or to describe something in",
      "front of the camera, emit one line [[EVA_LOOK]]{\"question\":\"<what to look for>\"}[[/EVA_LOOK]] (the",
      "question is optional). A frame is captured and you describe it. Do NOT claim you cannot see or use a",
      "camera. Emit at most one EVA_LOOK per reply, only when the user asks you to look or about what you see.",
      "SCHEDULED TASKS: Eva has a cron scheduler. When the user asks to schedule something recurring",
      "(daily briefings, periodic checks, reminders), acknowledge that this can be set up in Settings > Cron.",
      "SUBAGENTS: For multi-part tasks that can run in parallel, Eva can spawn isolated subagents. Each runs",
      "its own prompt concurrently and delivers results via notifications when done.",
      "SKILLS: Eva learns from successful complex tasks. After finishing a browser or desktop task, she may",
      "auto-extract a reusable skill. Skills are stored as drafts for user review in Settings > Goals/Skills.",
      "Do NOT narrate phases. Do NOT mention the pipeline, the reviewer,",
      "or any '.github/agents/' file. Do NOT print fake 'PHASE 1 / PHASE 2 / PHASE 3' headers.",
      "Just answer the user."
    ].join(' '),

    reviewer: [
      "You are Eva's Reviewer agent inside Eva's runtime cognitive layer.",
      "You critique Eva's draft against the user's actual request.",
      "Approve by default. Only request changes for MATERIAL problems: a factual",
      "or numeric error, an unsafe suggestion, a missed or misread part of the",
      "question, a leaked internal pipeline mention, hallucinated phase narration,",
      "or a missing/malformed required action block ([[EVA_ACTION]]...[[/EVA_ACTION]])",
      "when the user asked for a downloadable artifact.",
      "Do NOT request changes for style, tone, length, phrasing, or minor polish you",
      "merely prefer. If the draft is accurate, safe, and answers the question, APPROVE it.",
      "Always respond with a verdict line first:",
      "VERDICT: APPROVE  or  VERDICT: REQUEST_CHANGES",
      "If requesting changes, follow with concrete bullets naming the specific defect.",
      "Do not rewrite the answer."
    ].join(' ')
  };

  // Phrases that explicitly ask Eva to use her cognitive layer for this turn,
  // even if the toggle in Settings is off. Kept narrow to avoid false positives.
  var TRIGGER_PATTERNS = [
    /\btrigger\s+the\s+(cognitive\s+)?chain\b/i,
    /\buse\s+(the\s+)?cognition\b/i,
    /\buse\s+(the\s+)?cognitive\s+layer\b/i,
    /\brun\s+(the\s+)?(eva|reviewer)\b/i,
    /\brun\s+(the\s+)?(cognitive\s+)?(chain|pipeline)\b/i,
    /\bengage\s+cognition\b/i,
    /\bcognition\s*:\s*on\b/i
  ];

  function detectTrigger(text) {
    var s = String(text || '');
    for (var i = 0; i < TRIGGER_PATTERNS.length; i++) {
      if (TRIGGER_PATTERNS[i].test(s)) return true;
    }
    return false;
  }

  // Returns { active: bool, reason: 'toggle' | 'phrase' | null }
  function shouldRun(userMessage) {
    if (isEnabled()) return { active: true, reason: 'toggle' };
    if (detectTrigger(userMessage)) return { active: true, reason: 'phrase' };
    return { active: false, reason: null };
  }

  function ls(key, fallback) {
    try {
      var v = localStorage.getItem(key);
      return (v == null) ? fallback : v;
    } catch (_) { return fallback; }
  }

  function lsSet(key, value) {
    try { localStorage.setItem(key, value); } catch (_) {}
  }

  function getDefaultModel() {
    var el = document.getElementById('selAIGBackend');
    return (el && el.value) ? el.value : 'claude-sonnet-4.6';
  }

  function getCfg() {
    var def = getDefaultModel();
    return {
      enabled: ls('cogEnabled', '1') === '1',
      evaModel:      ls('cogEvaModel', '')      || def,
      reviewerModel: ls('cogReviewerModel', '') || def,
      maxCycles: Math.max(0, parseInt(ls('cogMaxCycles', '1'), 10) || 0),
      evaPrompt:      ls('cogEvaPrompt', '')      || DEFAULT_PROMPTS.eva,
      reviewerPrompt: ls('cogReviewerPrompt', '') || DEFAULT_PROMPTS.reviewer,
      showTrace: ls('cogShowTrace', '0') === '1'
    };
  }

  function setCfg(partial) {
    if (!partial) return;
    var map = {
      enabled: 'cogEnabled',
      evaModel: 'cogEvaModel',
      reviewerModel: 'cogReviewerModel',
      maxCycles: 'cogMaxCycles',
      evaPrompt: 'cogEvaPrompt',
      reviewerPrompt: 'cogReviewerPrompt',
      showTrace: 'cogShowTrace'
    };
    Object.keys(partial).forEach(function (k) {
      if (!map[k]) return;
      var v = partial[k];
      if (typeof v === 'boolean') v = v ? '1' : '0';
      lsSet(map[k], String(v == null ? '' : v));
    });
  }

  function isEnabled() { return getCfg().enabled; }

  function bridgeUrl() {
    return (typeof getACPBridgeUrl === 'function') ? getACPBridgeUrl() : 'http://localhost:8888';
  }

  function authPat() {
    return (typeof getAuthKey === 'function') ? getAuthKey('GITHUB_PAT') : '';
  }

  function status(text, kind) {
    if (typeof setStatus === 'function') {
      setStatus(kind || 'info', text);
    }
  }

  // ---------------------------------------------------------------------------
  // Capability registry (future actions)
  // ---------------------------------------------------------------------------
  // Shape: { id: 'string', description: 'string', run: async function(args) }
  // The eva agent receives the list of registered capability descriptions in
  // its system prompt so it can plan and invoke them. For now this is a
  // stub so feature work has a stable contract.
  var capabilities = [];

  function registerCapability(spec) {
    if (!spec || !spec.id || typeof spec.run !== 'function') return false;
    // Replace existing with same id so reload is safe
    capabilities = capabilities.filter(function (c) { return c.id !== spec.id; });
    capabilities.push({
      id: String(spec.id),
      description: String(spec.description || ''),
      run: spec.run
    });
    return true;
  }

  function listCapabilities() { return capabilities.slice(); }

  function describeCapabilities() {
    if (!capabilities.length) return '(no capabilities registered yet)';
    return capabilities.map(function (c) {
      return '- ' + c.id + ': ' + c.description;
    }).join('\n');
  }

  // ---------------------------------------------------------------------------
  // Action protocol
  // ---------------------------------------------------------------------------
  // The eva agent can emit blocks of the form:
  //   [[EVA_ACTION]]
  //   {"id": "file.download", "args": {...}}
  //   [[/EVA_ACTION]]
  // The browser parses each block, runs the matching capability, and replaces
  // the block with the capability's HTML output (or an inline error).
  // Match both properly closed blocks and unclosed ones (local models often
  // forget the closing tag). The second alternation grabs to end-of-string.
  var ACTION_BLOCK_RE = /\[\[EVA_ACTION\]\]([\s\S]*?)\[\[\/EVA_ACTION\]\]|\[\[EVA_ACTION\]\]([\s\S]+)$/g;

  async function executeActions(text) {
    if (!text) return { content: '', actions: [] };
    var actions = [];
    var out = text;
    var match;
    var replacements = [];
    ACTION_BLOCK_RE.lastIndex = 0;
    while ((match = ACTION_BLOCK_RE.exec(text)) !== null) {
      // Group 1 = closed block body, group 2 = unclosed block body
      replacements.push({ full: match[0], body: match[1] || match[2], index: match.index });
    }
    for (var i = 0; i < replacements.length; i++) {
      var r = replacements[i];
      var spec;
      try { spec = JSON.parse(String(r.body || '').trim()); }
      catch (e) {
        actions.push({ ok: false, error: 'invalid-json', detail: e.message });
        // Silently remove the malformed block instead of showing the raw
        // JSON error to the user. Local models often produce broken JSON.
        out = out.replace(r.full, '');
        continue;
      }
      var cap = capabilities.filter(function (c) { return c.id === spec.id; })[0];
      if (!cap) {
        actions.push({ ok: false, error: 'unknown-capability', id: spec.id });
        // Silently remove — local models often hallucinate capability IDs.
        out = out.replace(r.full, '');
        continue;
      }
      try {
        var result = await cap.run(spec.args || {});
        actions.push({ ok: true, id: spec.id, result: result });
        var html = (result && typeof result.html === 'string') ? result.html :
                   '<div class="cog-action-ok">[action ' + spec.id + ' completed]</div>';
        out = out.replace(r.full, html);
      } catch (err) {
        var msg = (err && err.message) ? err.message : String(err);
        actions.push({ ok: false, id: spec.id, error: 'run-failed', detail: msg });
        out = out.replace(r.full,
          '<div class="cog-action-err">[action ' + spec.id + ' failed: ' +
          String(msg).replace(/</g,'&lt;') + ']</div>');
      }
    }
    // Strip fake [Data Retrieved] sections that local models hallucinate.
    // Real data is injected into the system prompt, never the response.
    out = out.replace(/\[Data Retrieved\][^\[]*(?=\n\n|\n$|$)/gs, '');
    return { content: out.trim(), actions: actions };
  }

  // ---------------------------------------------------------------------------
  // Default capabilities
  // ---------------------------------------------------------------------------
  // file.download: deliver a downloadable artifact to the user. Args:
  //   filename: string  (required)
  //   content:  string  (required) - the file body
  //   mime:     string  (optional, default 'text/plain')
  // Returns { html } where html is a real <a download> link rendered inline.
  //
  // Artifacts are namespaced under a virtual path tmp/<session_id>/<filename>.
  // Browsers strip path separators from the download attribute for security,
  // so the link's effective filename is tmp__<sid8>__<filename>. The repo
  // .gitignore excludes tmp/ so any local mirroring stays out of git.
  function _shortSessionId() {
    try {
      if (typeof _activeSessionId === 'function') {
        var sid = _activeSessionId();
        if (sid) return String(sid).replace(/[^A-Za-z0-9_\-]/g, '').slice(0, 12) || 'nosess';
      }
    } catch (_) {}
    return 'nosess';
  }

  // Minimal, dependency-free PDF generator for text content. Produces a valid
  // multi-page PDF using the standard Helvetica font (no embedding needed), so
  // any viewer opens it. Earlier the file.download capability just labeled raw
  // text as application/pdf, which yielded a corrupt file. Latin-1 only: the
  // structural bytes are ASCII and text is mapped to Latin-1 so string length
  // equals byte length, which keeps the xref byte offsets correct.
  function _textToPdf(text, opts) {
    opts = opts || {};
    var fontSize = opts.fontSize || 11;
    var leading = Math.round(fontSize * 1.35);
    var marginX = 50, marginTop = 50;
    var pageW = 612, pageH = 792;
    var linesPerPage = Math.max(1, Math.floor((pageH - marginTop * 2) / leading));
    var maxChars = opts.wrap || 95;

    function toLatin1(s) {
      var o = '';
      for (var i = 0; i < s.length; i++) {
        var c = s.charCodeAt(i);
        o += (c <= 255) ? s.charAt(i) : '?';
      }
      return o;
    }
    function escPdf(s) {
      return s.replace(/\\/g, '\\\\').replace(/\(/g, '\\(').replace(/\)/g, '\\)');
    }

    // Word-wrap each source line to an approximate character width.
    var raw = String(text == null ? '' : text).replace(/\r\n?/g, '\n').split('\n');
    var lines = [];
    raw.forEach(function (ln) {
      ln = ln.replace(/\t/g, '    ');
      if (!ln) { lines.push(''); return; }
      var cur = '';
      ln.split(/(\s+)/).forEach(function (tok) {
        if (cur.length && (cur + tok).length > maxChars) {
          lines.push(cur);
          cur = /^\s+$/.test(tok) ? '' : tok;
        } else {
          cur += tok;
        }
        while (cur.length > maxChars) {
          lines.push(cur.slice(0, maxChars));
          cur = cur.slice(maxChars);
        }
      });
      lines.push(cur);
    });
    if (!lines.length) lines.push('');

    var pages = [];
    for (var i = 0; i < lines.length; i += linesPerPage) {
      pages.push(lines.slice(i, i + linesPerPage));
    }

    // Object plan: 1 Catalog, 2 Pages, 3 Font, then a (page, content) pair each.
    var objs = {};
    objs[1] = '<< /Type /Catalog /Pages 2 0 R >>';
    objs[3] = '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>';
    var pageNums = [], num = 4;
    pages.forEach(function (pl) {
      var pn = num++, cn = num++;
      pageNums.push(pn);
      var startY = pageH - marginTop;
      var stream = 'BT /F1 ' + fontSize + ' Tf ' + leading + ' TL ' + marginX + ' ' + startY + ' Td\n';
      pl.forEach(function (l) { stream += '(' + escPdf(toLatin1(l)) + ') Tj T*\n'; });
      stream += 'ET';
      objs[cn] = '<< /Length ' + stream.length + ' >>\nstream\n' + stream + '\nendstream';
      objs[pn] = '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 ' + pageW + ' ' + pageH +
                 '] /Resources << /Font << /F1 3 0 R >> >> /Contents ' + cn + ' 0 R >>';
    });
    objs[2] = '<< /Type /Pages /Kids [' +
              pageNums.map(function (n) { return n + ' 0 R'; }).join(' ') +
              '] /Count ' + pageNums.length + ' >>';

    var maxNum = num - 1;
    var out = '%PDF-1.4\n';
    var offsets = {};
    for (var n = 1; n <= maxNum; n++) {
      offsets[n] = out.length;
      out += n + ' 0 obj\n' + objs[n] + '\nendobj\n';
    }
    var xrefPos = out.length;
    out += 'xref\n0 ' + (maxNum + 1) + '\n0000000000 65535 f \n';
    for (var m = 1; m <= maxNum; m++) {
      out += ('0000000000' + offsets[m]).slice(-10) + ' 00000 n \n';
    }
    out += 'trailer\n<< /Size ' + (maxNum + 1) + ' /Root 1 0 R >>\nstartxref\n' + xrefPos + '\n%%EOF';
    return out;
  }

  // Convert a Latin-1/ASCII string to a byte array so Blob does not re-encode
  // it as UTF-8 (which would shift the PDF byte offsets and corrupt the file).
  function _latin1Bytes(str) {
    var bytes = new Uint8Array(str.length);
    for (var i = 0; i < str.length; i++) bytes[i] = str.charCodeAt(i) & 0xff;
    return bytes;
  }

  registerCapability({
    id: 'file.download',
    description: 'Deliver a downloadable artifact (text, markdown, csv, or a real PDF). ' +
                 'args: {filename:string, content:string, mime?:string}. Use mime ' +
                 '"application/pdf" or a .pdf filename to produce a genuine PDF. ' +
                 'The artifact renders inline as Download/Open links and PDFs auto-open ' +
                 'in a viewer tab. To let the user view it again, tell them to click the ' +
                 'Open or Download link in your message; do NOT say you cannot open files.',
    run: async function (args) {
      args = args || {};
      var safeName = String(args.filename || 'eva-artifact.txt')
                       .replace(/[^A-Za-z0-9._\-]+/g, '_').slice(0, 120) || 'eva-artifact.txt';
      var content = String(args.content == null ? '' : args.content);
      var mime = String(args.mime || 'text/plain');
      var isPdf = /application\/pdf/i.test(mime) || /\.pdf$/i.test(safeName);
      if (isPdf) {
        mime = 'application/pdf';
        if (!/\.pdf$/i.test(safeName)) safeName += '.pdf';
      }

      // Write the file through the bridge so it lands in ARTIFACTS_DIR and is
      // served via /v1/files/<name>. This replaces the old blob URL approach
      // which broke under Electron's file:// origin.
      var bUrl = bridgeUrl().replace(/\/+$/, '');
      try {
        var writeResp = await fetch(bUrl + '/v1/files/write', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ filename: safeName, content: content, is_pdf: isPdf })
        });
        if (!writeResp.ok) throw new Error('HTTP ' + writeResp.status);
      } catch (e) {
        return { html: '<div class="cog-action-err">[file write failed: ' + String(e.message || e) + ']</div>' };
      }

      // Emit [[EVA_FILE]] marker so renderEvaResponse appends proper download/open links.
      return {
        html: '\n[[EVA_FILE]] ' + safeName,
        filename: safeName,
        mime: mime
      };
    }
  });

  registerCapability({
    id: 'file.open',
    description: 'Open an existing artifact file that was already created in this conversation. ' +
                 'args: {filename:string}. Use this when the user asks to open, view, or show ' +
                 'a file that was already created via file.download. Do NOT recreate the file.',
    run: async function (args) {
      args = args || {};
      var filename = String(args.filename || '').replace(/[^A-Za-z0-9._\-]+/g, '_').slice(0, 120);
      if (!filename) {
        return { html: '<div class="cog-action-err">[file.open: no filename provided]</div>' };
      }
      var bUrl = bridgeUrl().replace(/\/+$/, '');
      try {
        var resp = await fetch(bUrl + '/v1/files/' + encodeURIComponent(filename) + '?open=1');
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        var data = await resp.json();
        if (data.opened) {
          return { html: '<div class="cog-action-ok">[Opened ' + filename + ']</div>' };
        }
        throw new Error('open returned false');
      } catch (e) {
        return { html: '<div class="cog-action-err">[Could not open ' + filename + ': ' + String(e.message || e) + ']</div>' };
      }
    }
  });

  // ---------------------------------------------------------------------------
  // Bridge call primitive
  // ---------------------------------------------------------------------------
  async function callAgent(role, model, systemPrompt, conversation, taskMessage, extra) {
    var url = bridgeUrl().replace(/\/+$/, '') + '/v1/aig/chat';
    var msgs = [{ role: 'system', content: systemPrompt }];
    if (Array.isArray(conversation) && conversation.length) {
      // Strip any prior system messages so each agent's framing is its own.
      msgs = msgs.concat(conversation.filter(function (m) { return m && m.role !== 'system'; }));
    }
    if (taskMessage) {
      msgs.push({ role: 'user', content: taskMessage });
    }
    var payload = {
      messages: msgs,
      user_message: taskMessage || '',
      model: model,
      lmstudio_base_url: (typeof getLmStudioBaseUrl === 'function') ? getLmStudioBaseUrl() : '',
      lmstudio_model: (typeof getLmStudioModel === 'function') ? getLmStudioModel() : '',
      github_pat: authPat(),
      internal: true
    };
    if (extra && typeof extra === 'object') {
      Object.keys(extra).forEach(function (k) { payload[k] = extra[k]; });
    }
    var resp = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (!resp.ok) {
      var t = '';
      try { t = await resp.text(); } catch (_) {}
      throw new Error(role + ' (' + model + ') HTTP ' + resp.status + (t ? ': ' + t : ''));
    }
    var data = await resp.json();
    var content = (data.choices && data.choices[0] && data.choices[0].message && data.choices[0].message.content) || '';
    return { content: content, model: data.model || model };
  }

  // Fire-and-forget telemetry. Stores a local ring buffer (last 50 turns) and
  // best-effort posts the same record to the bridge so it lands in the shared
  // JSONL log. Only timings/labels are sent, never message or response text.
  function postTelemetry(record) {
    try {
      var key = 'cog_telemetry';
      var ring = [];
      try { ring = JSON.parse(localStorage.getItem(key) || '[]'); } catch (_) { ring = []; }
      if (!Array.isArray(ring)) ring = [];
      ring.push(Object.assign({ ts: Date.now() }, record));
      if (ring.length > 50) ring = ring.slice(-50);
      localStorage.setItem(key, JSON.stringify(ring));
    } catch (_) {}
    try {
      var url = bridgeUrl().replace(/\/+$/, '') + '/v1/telemetry';
      fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(record)
      }).catch(function () {});
    } catch (_) {}
  }

  function parseVerdict(text) {
    var s = String(text || '');
    var m = s.match(/VERDICT\s*:\s*(APPROVE|REQUEST[_\- ]?CHANGES|BLOCKED)/i);
    if (m) {
      var v = m[1].toUpperCase().replace(/[_\- ]/g, '_');
      if (v === 'APPROVE') return 'APPROVE';
      if (v === 'BLOCKED') return 'BLOCKED';
      return 'REQUEST_CHANGES';
    }
    if (/^\s*APPROVE\b/im.test(s)) return 'APPROVE';
    if (/^\s*BLOCKED\b/im.test(s)) return 'BLOCKED';
    return 'REQUEST_CHANGES';
  }

  // Eva's silent self-review signal. The draft appends
  // [[REVIEW]]{"want":bool,"reason":"..."}[[/REVIEW]] indicating whether a
  // second-opinion review would help. Parsed out and never shown to the user.
  var REVIEW_SENTINEL_RE = /\[\[REVIEW\]\]\s*([\s\S]*?)\s*\[\[\/REVIEW\]\]/i;

  function parseReviewSentinel(text) {
    var s = String(text == null ? '' : text);
    var m = s.match(REVIEW_SENTINEL_RE);
    var want = null;
    var reason = '';
    if (m) {
      var bodyStr = (m[1] || '').trim();
      try {
        var obj = JSON.parse(bodyStr);
        if (obj && typeof obj === 'object') {
          want = (obj.want === true || obj.want === 'true');
          reason = String(obj.reason || '');
        }
      } catch (e) {
        if (/\bwant\b\s*[:=]\s*true/i.test(bodyStr)) want = true;
        else if (/\bwant\b\s*[:=]\s*false/i.test(bodyStr)) want = false;
      }
    }
    var cleaned = s.replace(REVIEW_SENTINEL_RE, '').replace(/\n{3,}/g, '\n\n').trim();
    return { present: !!m, want: want, reason: reason, cleaned: cleaned };
  }

  // Deterministic review floor: turns where a second opinion is mandatory and
  // Eva cannot opt out. Two intentionally small, legible buckets keep this easy
  // to maintain. Edit a bucket here rather than scattering keywords.
  //
  //   FACTUAL_TOPICS   - subjects with real fabrication/staleness risk.
  //   RETRIEVAL_INTENT - phrases that signal the user wants current/looked-up
  //                      info. Kept tight (no bare "find"/"today"/"events")
  //                      to avoid false positives on ordinary conversation.
  var FACTUAL_TOPICS = /\b(brief(ing)?|brief me|news|headlines?|stocks?|prices?|quote|markets?|nasdaq|dow|s&p|weather|forecast|filings?|earnings|ticker|economy|economic)\b/i;
  var RETRIEVAL_INTENT = /\b(look(ing)?\s*(it\s*)?up|search(\s+for)?|google|find\s+out|latest|most\s+recent|breaking|what'?s\s+(happening|going\s+on|new)|right\s+now)\b/i;

  function reviewFloorReason(userMsg, draftContent) {
    if (/\[\[EVA_ACTION\]\]|\[\[EVA_BROWSER\]\]|\[\[EVA_DESKTOP\]\]|\[\[EVA_LOOK\]\]|\[\[EVA_FILE\]\]/i.test(String(draftContent || ''))) {
      return 'action';
    }
    var u = String(userMsg || '');
    if (FACTUAL_TOPICS.test(u) || RETRIEVAL_INTENT.test(u)) {
      return 'factual';
    }
    return '';
  }

  // ---------------------------------------------------------------------------
  // Pipeline: eva -> (reviewer -> eva)*
  // ---------------------------------------------------------------------------
  // opts:
  //   userMessage : string  (required) the raw user turn
  //   messages    : array   prior conversation [{role, content}, ...]
  //
  // returns: { content, trace, evaModel, reviewerModel, cycles }
  async function run(opts) {
    opts = opts || {};
    var cfg = getCfg();
    var userMsg = String(opts.userMessage || '').trim();
    var convo = Array.isArray(opts.messages) ? opts.messages.slice() : [];
    var trace = [];
    var _turnStart = Date.now();
    var _draftMs = 0, _reviewMs = 0, _reviseMs = 0;
    var capDesc = describeCapabilities();

    var actionHelp = [
      '',
      'Action protocol:',
      'To actually perform a registered capability, emit a block on its own line:',
      '[[EVA_ACTION]]',
      '{"id":"<capability-id>","args":{...}}',
      '[[/EVA_ACTION]]',
      'The browser will execute it and replace the block with the rendered result.',
      'Use file.download ONLY when the user explicitly asks for a file, document, PDF, report, or download.',
      'Default: answer inline in chat. Do NOT auto-generate file artifacts unless the user asks for one.'
    ].join('\n');

    // Stage 1: Eva plans and drafts the user-facing answer
    status('Eva drafting [eva: ' + cfg.evaModel + ']...');
    var draftTask = [
      'User message:',
      userMsg,
      '',
      'Registered capabilities you can invoke (or empty if none):',
      capDesc,
      actionHelp,
      '',
      'Write the user-facing answer now.',
      'IMPORTANT: By default, answer INLINE in the chat. Present briefings, reports, and summaries',
      'as formatted text in your response. Only emit a [[EVA_ACTION]] file.download block when the',
      'user EXPLICITLY asks for a file, document, PDF, report download, or specific file format.',
      'Phrases like "give me a briefing" or "what\'s the news" = answer inline.',
      'Phrases like "create a PDF report" or "generate a markdown file" or "make a document" = file.download.',
      'When the user asks to OPEN or VIEW a file that was already created earlier in this conversation,',
      'use file.open with the existing filename. Do NOT recreate the file as a PDF or any other format.',
      'Never simulate or describe phases. Never print PHASE headers. Just answer.',
      '',
      'After your answer, on the very last line, append a SILENT self-review signal the user never sees:',
      '[[REVIEW]]{"want":true|false,"reason":"<=12 words"}[[/REVIEW]]',
      'Set want=true when a second-opinion review by another model would meaningfully improve accuracy,',
      'safety, or completeness (factual claims, anything important or easy to get wrong). Set want=false',
      'for simple, low-stakes, or purely conversational replies. This line is stripped before display.'
    ].join('\n');
    var draft = await callAgent(
      'eva', cfg.evaModel, cfg.evaPrompt, convo, draftTask,
      { inject_memory: true, recall_query: userMsg, retrieve_data: true }
    );

    // Eva's silent self-review signal decides whether a second opinion runs.
    var sentinel = parseReviewSentinel(draft.content);
    var current = sentinel.cleaned;
    trace.push({ role: 'eva', model: draft.model, content: current });
    _draftMs = Date.now() - _turnStart;
    var _draftChars = current.length;

    var cyclesUsed = 0;
    var lastVerdict = 'APPROVE';

    // Review gate: a deterministic floor forces review on irreversible or
    // fact-bearing turns; above the floor, Eva can opt in via her sentinel.
    // Everything else takes the fast path and skips review+revise. (We used to
    // default a missing signal to "review anyway", but telemetry showed the
    // sentinel is effectively never emitted, so every basic chat turn paid the
    // full draft+review+revise cost. The floor still guarantees review on the
    // high-fabrication-risk and irreversible categories.)
    var floorReason = reviewFloorReason(userMsg, current);
    var reviewReason;
    if (floorReason) {
      reviewReason = 'floor:' + floorReason;
    } else if (sentinel.want === true) {
      reviewReason = 'eva-opt-in';
    } else {
      reviewReason = '';
    }
    var doReview = cfg.maxCycles >= 1 && !!reviewReason;
    if (!doReview) {
      var skipWhy = cfg.maxCycles < 1 ? 'disabled'
                  : (sentinel.want === false ? 'eva-opt-out' : 'fast-path');
      status('Eva answering directly [no review: ' + skipWhy + ']...');
    }

    // Stage 2+: reviewer loop, bounded by cfg.maxCycles, gated by doReview
    for (var cycle = 1; doReview && cycle <= cfg.maxCycles; cycle++) {
      cyclesUsed = cycle;
      status('Eva reviewing [reviewer: ' + cfg.reviewerModel + '] cycle ' + cycle + '/' + cfg.maxCycles + '...');
      var reviewTask = [
        'User message:',
        userMsg,
        '',
        'Eva draft:',
        current,
        '',
        'Review the draft. First line MUST be either:',
        'VERDICT: APPROVE',
        'VERDICT: REQUEST_CHANGES',
        'Approve by default. Only REQUEST_CHANGES for a material accuracy, safety,',
        'or completeness defect (a wrong fact/number, an unsafe suggestion, or a',
        'missed part of the question). Do not request changes for style, tone,',
        'length, or wording you merely prefer.',
        'If requesting changes, follow with concrete bullet points naming each defect.'
      ].join('\n');
      var _revStart = Date.now();
      var review;
      try {
        review = await callAgent(
          'reviewer', cfg.reviewerModel, cfg.reviewerPrompt, convo, reviewTask,
          { no_tools: true }
        );
      } catch (reviewErr) {
        // Review failed (timeout, network). Skip review and use the draft as-is.
        console.warn('[Cognition] Review failed, using draft:', reviewErr.message);
        break;
      }
      _reviewMs += Date.now() - _revStart;
      var verdict = parseVerdict(review.content);
      lastVerdict = verdict;
      trace.push({
        role: 'reviewer', model: review.model, content: review.content,
        cycle: cycle, verdict: verdict
      });
      if (verdict === 'APPROVE' || verdict === 'BLOCKED') break;

      // Eva revises against reviewer feedback
      status('Eva revising [eva: ' + cfg.evaModel + '] cycle ' + cycle + '...');
      var reviseTask = [
        'User message:',
        userMsg,
        '',
        'Previous draft:',
        current,
        '',
        'Reviewer feedback:',
        review.content,
        '',
        'Registered capabilities:',
        capDesc,
        actionHelp,
        '',
        'Produce the revised final answer for the user. Apply the reviewer\'s concrete points.',
        'Do not mention the review process or any internal pipeline.'
      ].join('\n');
      var _reviseStart = Date.now();
      var revised;
      try {
        revised = await callAgent(
          'eva', cfg.evaModel, cfg.evaPrompt, convo, reviseTask,
          { inject_memory: true, recall_query: userMsg }
        );
      } catch (reviseErr) {
        // Revise failed (timeout, network error). Fall back to the draft
        // rather than surfacing a raw error message to the user.
        console.warn('[Cognition] Revise failed, using draft:', reviseErr.message);
        trace.push({ role: 'eva', model: cfg.evaModel, content: '[revise failed: ' + reviseErr.message + ']', cycle: cycle, revised: true });
        break;
      }
      _reviseMs += Date.now() - _reviseStart;
      // Strip any sentinel the revision may have re-emitted.
      var revisedClean = parseReviewSentinel(revised.content).cleaned;
      trace.push({
        role: 'eva', model: revised.model, content: revisedClean,
        cycle: cycle, revised: true
      });
      current = revisedClean;
    }

    // Privacy-safe telemetry: stage timings, models, and the gate decision.
    // No message or response text is sent, only counts and labels.
    var telem = {
      turn_ms: Date.now() - _turnStart,
      draft_ms: _draftMs,
      review_ms: _reviewMs,
      revise_ms: _reviseMs,
      cycles: cyclesUsed,
      draft_chars: _draftChars,
      final_chars: (current || '').length,
      eva_model: draft.model || cfg.evaModel,
      reviewer_model: doReview ? cfg.reviewerModel : '',
      review_reason: reviewReason || (sentinel.want === false ? 'eva-opt-out' : 'fast-path'),
      last_verdict: doReview ? lastVerdict : 'n/a',
      sentinel_want: (sentinel.want === null ? 'absent' : String(sentinel.want))
    };
    postTelemetry(telem);

    return {
      content: current,
      trace: trace,
      evaModel: draft.model,
      reviewerModel: cfg.reviewerModel,
      cycles: cyclesUsed,
      lastVerdict: lastVerdict,
      reviewed: doReview,
      reviewReason: reviewReason || (sentinel.want === false ? 'eva-opt-out' : 'fast-path'),
      telemetry: telem,
      forced: !!opts.forceEnable,
      forcedReason: opts.forcedReason || null
    };
  }

  // ---------------------------------------------------------------------------
  // Trace rendering helper (optional, off by default)
  // ---------------------------------------------------------------------------
  function renderTraceHtml(trace) {
    if (!Array.isArray(trace) || !trace.length) return '';
    var esc = (typeof escapeHtml === 'function') ? escapeHtml : function (s) {
      return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    };
    var parts = ['<details class="cog-trace"><summary>Cognition trace (',
                 String(trace.length), ' steps)</summary>'];
    trace.forEach(function (step, i) {
      var label = (step.role || 'step') +
                  (step.cycle ? ' #' + step.cycle : '') +
                  (step.revised ? ' (revised)' : '') +
                  (step.verdict ? ' [' + step.verdict + ']' : '');
      parts.push('<div class="cog-step"><div class="cog-step-head">' +
                 esc(label) + ' <span class="cog-step-model">' +
                 esc(step.model || '') + '</span></div>' +
                 '<pre class="cog-step-body">' + esc(step.content) + '</pre></div>');
    });
    parts.push('</details>');
    return parts.join('');
  }

  global.EvaCognition = global.EvaCognition || {};
  global.EvaCognition.DEFAULT_PROMPTS = {
    eva: DEFAULT_PROMPTS.eva,
    reviewer: DEFAULT_PROMPTS.reviewer
  };

  global.Cognition = {
    run: run,
    isEnabled: isEnabled,
    shouldRun: shouldRun,
    detectTrigger: detectTrigger,
    getCfg: getCfg,
    setCfg: setCfg,
    DEFAULT_PROMPTS: DEFAULT_PROMPTS,
    registerCapability: registerCapability,
    listCapabilities: listCapabilities,
    describeCapabilities: describeCapabilities,
    executeActions: executeActions,
    renderTraceHtml: renderTraceHtml
  };
})(window);
