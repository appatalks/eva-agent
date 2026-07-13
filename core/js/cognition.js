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
      "You can request an isolated Playwright browser run with a mandatory closed marker. Include a deterministic",
      "request postcondition when known, for example [[EVA_BROWSER]]{\"goal\":\"<task>\",\"start_url\":\"<public url>\",",
      "\"postcondition\":{\"type\":\"browser.url_match\",\"origin\":\"<public origin>\",\"path\":\"/expected\"}}[[/EVA_BROWSER]].",
      "Electron asks the user to authorize the complete launch, and every click, navigation, scroll, or exact-field",
      "entry requires a separate approval. A model completion claim is not success; only a causal tool-observed",
      "postcondition transition is verified. Never emit browser and desktop markers together.",
      "Desktop control is launch-only in this release. To request an allowlisted GUI launch, emit",
      "[[EVA_DESKTOP]]{\"goal\":\"open <app>\",\"postcondition\":{\"type\":\"desktop.process_spawned\",",
      "\"executable\":\"<allowlisted binary>\",\"state\":\"started\"}}[[/EVA_DESKTOP]].",
      "Desktop pointer, keyboard, shell, arguments, window focus, and arbitrary file-open authority are unavailable",
      "until the capability broker is implemented. Say so plainly when the requested operation exceeds this scope.",
      "Before an allowed marker, write one short sentence describing the bounded request. Never claim it completed",
      "until the typed outcome is verified. If no deterministic postcondition is available, the result remains",
      "unverified and must be described that way.",
      "Only after the user explicitly asks to use or look through their camera/webcam, emit one standalone",
      "mandatory closed [[EVA_LOOK]]{\"question\":\"<what to look for>\"}[[/EVA_LOOK]] proposal.",
      "Electron separately asks the user to authorize one fresh frame. Never use camera control for web search,",
      "shopping, or image requests. Emit exactly one control surface at most per response.",
      "Signal delivery is unavailable from model output. Trusted configured alerts use a separate path.",
      "SCHEDULED TASKS: Eva has a cron scheduler. When the user asks to schedule something recurring",
      "(daily briefings, periodic checks, reminders), acknowledge that this can be set up in Settings > Cron.",
      "SUBAGENTS: For multi-part tasks that can run in parallel, Eva can spawn isolated subagents. Each runs",
      "its own prompt concurrently and delivers results via notifications when done.",
      "LEARNING: governed Phase 3 experiments are default-off, local, shadow-only, and never activate behavior.",
      "The separately gated legacy skill-draft path requires explicit review; never claim automatic extraction.",
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
    if (!spec || !spec.id || typeof spec.validate !== 'function' ||
        typeof spec.run !== 'function') return false;
    // Replace existing with same id so reload is safe
    capabilities = capabilities.filter(function (c) { return c.id !== spec.id; });
    capabilities.push({
      id: String(spec.id),
      description: String(spec.description || ''),
      effectful: spec.effectful === true,
      validate: spec.validate,
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
  // The complete response is parsed before any capability validation or I/O.
  // Exactly one closed top-level control surface is allowed per response.
  var MAX_ACTIONS_PER_TURN = 1;
  var MAX_ACTION_BODY_BYTES = 32 * 1024;
  var MAX_ACTION_BATCH_BYTES = 32 * 1024;

  function parseWholeResponse(text) {
    if (typeof EvaAgentMarkers !== 'undefined' &&
        typeof EvaAgentMarkers.parseResponse === 'function') {
      return EvaAgentMarkers.parseResponse(text);
    }
    var source = String(text || '');
    var first = source.search(/\[\[\/?EVA_/);
    return {
      text: first < 0 ? source : source.slice(0, first).trim(),
      actions: [], invalid: first >= 0, conflict: first >= 0,
      browser: null, desktop: null, camera: null, signal: false
    };
  }

  function sanitizeActionText(text) {
    return parseWholeResponse(text).text;
  }

  function envelopeIsCurrent(envelope) {
    if (!envelope) return true;
    return typeof isCurrentRequestEnvelope === 'function' &&
      isCurrentRequestEnvelope(envelope);
  }

  function requireCurrentEnvelope(envelope) {
    if (envelopeIsCurrent(envelope)) return;
    var error = new Error('Stale request completion ignored');
    error.code = 'EVA_STALE_ENVELOPE';
    throw error;
  }

  async function executeActions(text, capturedEnvelope) {
    requireCurrentEnvelope(capturedEnvelope);
    if (!text) return { content: '', actions: [] };
    var protocol = parseWholeResponse(text);
    if (protocol.invalid || protocol.conflict) {
      return {
        content: protocol.text,
        actions: [{
          ok: false, id: 'control.response', error: 'invalid-control-response',
          detail: 'No control executed because the complete response was invalid.'
        }]
      };
    }
    if (!protocol.actions || protocol.actions.length === 0) {
      return { content: String(text), actions: [] };
    }
    var actions = [];
    var parsedActions = [];
    var batchBytes = 0;
    var effectCount = 0;
    var validationError = '';
    if (protocol.actions.length > MAX_ACTIONS_PER_TURN) {
      validationError = 'action-count-exceeded';
    }
    for (var preIndex = 0; preIndex < protocol.actions.length && !validationError; preIndex++) {
      var body = String(protocol.actions[preIndex].raw || '').trim();
      var bodyBytes = new TextEncoder().encode(body).byteLength;
      batchBytes += bodyBytes;
      if (bodyBytes === 0 || bodyBytes > MAX_ACTION_BODY_BYTES ||
          batchBytes > MAX_ACTION_BATCH_BYTES) {
        validationError = 'action-size-exceeded';
        break;
      }
      var parsedSpec = protocol.actions[preIndex].payload;
      if (!parsedSpec || typeof parsedSpec !== 'object' || Array.isArray(parsedSpec) ||
          Object.keys(parsedSpec).some(function(key) {
            return key !== 'id' && key !== 'args';
          }) || typeof parsedSpec.id !== 'string' ||
          !parsedSpec.args || typeof parsedSpec.args !== 'object' ||
          Array.isArray(parsedSpec.args)) {
        validationError = 'invalid-action-shape';
        break;
      }
      var parsedCap = capabilities.filter(function(capability) {
        return capability.id === parsedSpec.id;
      })[0];
      if (!parsedCap) {
        validationError = 'unknown-capability';
        break;
      }
      var normalizedArgs;
      try {
        normalizedArgs = parsedCap.validate(parsedSpec.args);
      } catch (_) {
        validationError = 'invalid-capability-arguments';
        break;
      }
      if (!normalizedArgs || typeof normalizedArgs !== 'object' ||
          Array.isArray(normalizedArgs) || typeof normalizedArgs.then === 'function') {
        validationError = 'invalid-capability-arguments';
        break;
      }
      if (parsedCap.effectful) effectCount += 1;
      if (effectCount > 1) {
        validationError = 'multiple-effects-unavailable';
        break;
      }
      parsedActions.push({
        spec: { id: parsedSpec.id, args: normalizedArgs }, cap: parsedCap
      });
    }
    if (validationError) {
      return {
        content: protocol.text,
        actions: [{
          ok: false, id: 'action.batch', error: validationError,
          detail: 'No actions executed because the complete batch was invalid.'
        }]
      };
    }
    for (var i = 0; i < parsedActions.length; i++) {
      var spec = parsedActions[i].spec;
      var cap = parsedActions[i].cap;
      try {
        requireCurrentEnvelope(capturedEnvelope);
        var result = await cap.run(spec.args || {}, {
          envelope: capturedEnvelope
        });
        requireCurrentEnvelope(capturedEnvelope);
        if (spec.id === 'file.download' && result &&
            typeof result.filename === 'string') {
            if (typeof recordTrustedArtifact !== 'function' ||
              !recordTrustedArtifact(
                result, result.registry_epoch, result.generation
              )) {
            throw new Error('artifact registry rejected the created file');
          }
        }
        actions.push({ ok: true, id: spec.id, result: result });
      } catch (err) {
        if (err && err.code === 'EVA_STALE_ENVELOPE') throw err;
        var msg = (err && err.message) ? err.message : String(err);
        actions.push({ ok: false, id: spec.id, error: 'run-failed', detail: msg });
      }
    }
    // Strip fake [Data Retrieved] sections that local models hallucinate.
    // Real data is injected into the system prompt, never the response.
    var out = protocol.text.replace(/\[Data Retrieved\][^\[]*(?=\n\n|\n$|$)/gs, '');
    return { content: out.trim(), actions: actions };
  }

  // ---------------------------------------------------------------------------
  // Default capabilities
  // ---------------------------------------------------------------------------
  // file.download: deliver a downloadable artifact to the user. Args:
  //   filename: string  (required)
  //   content:  string  (required) - the file body
  //   mime:     string  (optional, default 'text/plain')
  // Returns structured metadata; the response renderer builds links with DOM APIs.
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
    effectful: true,
    description: 'Deliver a downloadable artifact (text, markdown, csv, or a real PDF). ' +
                 'args: {filename:string, content:string, mime?:string}. Use mime ' +
                 '"application/pdf" or a .pdf filename to produce a genuine PDF. ' +
                 'The artifact renders as a manual Download control. To let the user view it again, tell them to click the ' +
                 'Download link in your message; server-side file opening is unavailable.',
    validate: function(args) {
      if (!args || typeof args !== 'object' || Array.isArray(args) ||
          Object.keys(args).some(function(key) {
            return key !== 'filename' && key !== 'content' && key !== 'mime';
          }) || typeof args.filename !== 'string' || !args.filename.trim() ||
          args.filename.length > 120 || typeof args.content !== 'string' ||
          (args.mime !== undefined && (typeof args.mime !== 'string' ||
            !args.mime.trim() || args.mime.length > 128))) {
        throw new Error('file.download arguments are invalid');
      }
      var safeName = args.filename
                       .replace(/[^A-Za-z0-9._\-]+/g, '_').slice(0, 120) || 'eva-artifact.txt';
      if (safeName === '.identity.json') {
        throw new Error('file.download filename is reserved');
      }
      var mime = args.mime || 'text/plain';
      var isPdf = /application\/pdf/i.test(mime) || /\.pdf$/i.test(safeName);
      if (isPdf) {
        mime = 'application/pdf';
        if (!/\.pdf$/i.test(safeName)) safeName += '.pdf';
      }
      return {
        filename: safeName, content: args.content, mime: mime, isPdf: isPdf
      };
    },
    run: async function (args, context) {
      var safeName = args.filename;
      var content = args.content;
      var mime = args.mime;
      var isPdf = args.isPdf;

      // Write the file through the bridge so it lands in ARTIFACTS_DIR and is
      // served via its immutable session/artifact/digest route. This replaces the old blob URL approach
      // which broke under Electron's file:// origin.
      var bUrl = bridgeUrl().replace(/\/+$/, '');
      var envelope = context && context.envelope;
      if (!envelope || typeof envelope.session_id !== 'string') {
        throw new Error('artifact creation requires a bound session');
      }
      try {
        var artifactEpoch = (typeof _artifactRegistryEpoch === 'function')
          ? _artifactRegistryEpoch() : 0;
        var generationResp = await fetch(bUrl + '/v1/files/generation');
        if (!generationResp.ok) throw new Error('generation HTTP ' + generationResp.status);
        var generationData = await generationResp.json();
        if (!generationData || typeof generationData.generation !== 'string' ||
          !/^[1-9][0-9]{0,39}$/.test(generationData.generation)) {
          throw new Error('bridge returned no artifact generation');
        }
        if (typeof _acceptArtifactServerGeneration !== 'function' ||
            !_acceptArtifactServerGeneration(generationData.generation).ok) {
          throw new Error('bridge artifact generation regressed');
        }
        var writeResp = await fetch(bUrl + '/v1/files/write', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            filename: safeName, content: content, is_pdf: isPdf,
            mime: mime, session_id: envelope.session_id,
            turn_id: envelope.turn_id,
            generation: generationData.generation
          })
        });
        if (!writeResp.ok) throw new Error('HTTP ' + writeResp.status);
        var artifact = await writeResp.json();
        if (!artifact || artifact.ok !== true ||
          artifact.generation !== generationData.generation ||
          (typeof currentArtifactServerGeneration === 'function' &&
           artifact.generation !== currentArtifactServerGeneration())) {
          throw new Error('bridge returned no artifact identity');
        }
      } catch (e) {
        throw new Error('file write failed: ' + String(e.message || e));
      }

      return {
        filename: artifact.filename,
        mime: artifact.mime,
        session_id: artifact.session_id,
        artifact_id: artifact.artifact_id,
        digest: artifact.digest,
        generation: artifact.generation,
        size: artifact.size,
        registry_epoch: artifactEpoch,
        notice: 'Created artifact ' + safeName
      };
    }
  });

  registerCapability({
    id: 'file.open',
    description: 'Surface an existing artifact file that was already created in this conversation. ' +
                 'args: {filename:string}. Use this when the user asks to open, view, or show ' +
                 'a file that was already created via file.download. Do NOT recreate the file.',
    validate: function(args) {
      if (!args || typeof args !== 'object' || Array.isArray(args) ||
          Object.keys(args).length !== 1 || typeof args.filename !== 'string' ||
          !args.filename.trim() || args.filename.length > 120) {
        throw new Error('file.open arguments are invalid');
      }
      var filename = args.filename.replace(/[^A-Za-z0-9._\-]+/g, '_').slice(0, 120);
      var artifact = (typeof getTrustedArtifact === 'function')
        ? getTrustedArtifact(filename) : null;
      if (!artifact) {
        throw new Error('file.open: filename is not in the trusted artifact registry');
      }
      return { filename: filename, artifact: artifact };
    },
    run: async function (args) {
      var filename = args.filename;
      var artifact = args.artifact;
      return {
        filename: artifact.filename,
        mime: artifact.mime,
        session_id: artifact.session_id,
        artifact_id: artifact.artifact_id,
        digest: artifact.digest,
        generation: artifact.generation,
        size: artifact.size,
        notice: 'Artifact ' + filename + ' is ready; use the Download link.'
      };
    }
  });

  // ---------------------------------------------------------------------------
  // Bridge call primitive
  // ---------------------------------------------------------------------------
  async function callAgent(role, model, systemPrompt, conversation, taskMessage, extra, capturedEnvelope) {
    requireCurrentEnvelope(capturedEnvelope);
    var url = bridgeUrl().replace(/\/+$/, '') + '/v1/aig/chat';
    var msgs = [{ role: 'system', content: systemPrompt }];
    if (Array.isArray(conversation) && conversation.length) {
      // Strip any prior system messages so each agent's framing is its own.
      msgs = msgs.concat(conversation.filter(function (m) { return m && m.role !== 'system'; }));
    }
    if (taskMessage) {
      msgs.push({ role: 'user', content: taskMessage });
    }
    var _callEnvelope = capturedEnvelope || ((typeof captureRequestEnvelope === 'function')
      ? captureRequestEnvelope()
      : {
          session_id: (typeof window !== 'undefined' && window._evaSessionId) ? window._evaSessionId : '',
          turn_id: (typeof window !== 'undefined' && window._evaTurnId) ? window._evaTurnId : ''
        });
    var payload = {
      messages: msgs,
      user_message: taskMessage || '',
      model: model,
      session_id: _callEnvelope.session_id,
      turn_id: _callEnvelope.turn_id,
      request_id: _callEnvelope.request_id,
      correlation_id: _callEnvelope.correlation_id,
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
    requireCurrentEnvelope(_callEnvelope);
    if (!resp.ok) {
      throw new Error(role + ' request failed (HTTP ' + resp.status + ').');
    }
    var data = await resp.json();
    requireCurrentEnvelope(_callEnvelope);
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
    if (/\[\[EVA_ACTION\]\]|\[\[EVA_BROWSER\]\]|\[\[EVA_DESKTOP\]\]|\[\[EVA_LOOK\]\]/i.test(String(draftContent || ''))) {
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
    var runEnvelope = opts.envelope || ((typeof captureRequestEnvelope === 'function') ? captureRequestEnvelope() : null);
    requireCurrentEnvelope(runEnvelope);
    var convo = Array.isArray(opts.messages) ? opts.messages.slice() : [];
    var trace = [];
    var _turnStart = Date.now();
    var _draftMs = 0, _reviewMs = 0, _reviseMs = 0;
    var capDesc = describeCapabilities();
    var trustedArtifacts = Array.isArray(opts.trustedArtifacts)
      ? opts.trustedArtifacts.slice(0, 32) : [];

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
      {
        inject_memory: true, recall_query: userMsg, retrieve_data: true,
        trusted_artifacts: trustedArtifacts
      }, runEnvelope
    );
    requireCurrentEnvelope(runEnvelope);

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
          { no_tools: true }, runEnvelope
        );
      } catch (reviewErr) {
        if (reviewErr && reviewErr.code === 'EVA_STALE_ENVELOPE') throw reviewErr;
        // Review failed (timeout, network). Skip review and use the draft as-is.
        console.warn('[Cognition] Review failed, using draft:', reviewErr.message);
        break;
      }
      requireCurrentEnvelope(runEnvelope);
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
          {
            inject_memory: true, recall_query: userMsg,
            trusted_artifacts: trustedArtifacts
          }, runEnvelope
        );
      } catch (reviseErr) {
        if (reviseErr && reviseErr.code === 'EVA_STALE_ENVELOPE') throw reviseErr;
        // Revise failed (timeout, network error). Fall back to the draft
        // rather than surfacing a raw error message to the user.
        console.warn('[Cognition] Revise failed, using draft:', reviseErr.message);
        trace.push({ role: 'eva', model: cfg.evaModel, content: '[revise failed: ' + reviseErr.message + ']', cycle: cycle, revised: true });
        break;
      }
      requireCurrentEnvelope(runEnvelope);
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
    requireCurrentEnvelope(runEnvelope);
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
                 '<pre class="cog-step-body">' + esc(
                   typeof canonicalizeEvaResponse === 'function'
                     ? canonicalizeEvaResponse(step.content).text
                     : sanitizeActionText(step.content)
                 ) + '</pre></div>');
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
    sanitizeActionText: sanitizeActionText,
    renderTraceHtml: renderTraceHtml
  };
})(window);
