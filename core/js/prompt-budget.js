// Framework-free prompt budgeting shared by every provider.
// Persistent histories stay in localStorage; this module only builds request views.
(function (global) {
  'use strict';

  var DEFAULT_BUDGET = 12000;
  var DEFAULT_RECENT_TURNS = 6;
  var PINNED_ROLES = { system: true, developer: true };
  var ACTION_RE = /\b(action|completed|failed|declined|cancelled|canceled|sent|created|opened|downloaded|ran|executed)\b|\[\[EVA_[A-Z_]+\]\]/i;
  var CORRECTION_RE = /\b(actually|correction|correct(?:ion)?|not that|i meant|wrong|incorrect|instead)\b/i;
  var UNRESOLVED_RE = /\b(todo|to do|pending|unresolved|still need|waiting|blocked|follow[- ]?up|unfinished|not yet)\b/i;

  function textOf(message) {
    if (!message) return '';
    if (typeof message.content === 'string') return message.content;
    if (Array.isArray(message.content)) {
      return message.content.map(function (part) {
        if (typeof part === 'string') return part;
        return part && (part.text || part.content || '') || '';
      }).join('\n');
    }
    if (Array.isArray(message.parts)) {
      return message.parts.map(function (part) {
        if (typeof part === 'string') return part;
        return part && (part.text || part.content || '') || '';
      }).join('\n');
    }
    return message.content == null ? '' : String(message.content);
  }

  function estimateTokens(value) {
    var text = String(value == null ? '' : value);
    if (!text) return 0;
    return Math.max(1, Math.ceil(text.length / 4));
  }

  function cloneMessage(message) {
    var copy = {};
    Object.keys(message || {}).forEach(function (key) {
      var value = message[key];
      copy[key] = Array.isArray(value) ? value.map(function (item) {
        return item && typeof item === 'object' ? Object.assign({}, item) : item;
      }) : value;
    });
    return copy;
  }

  function isPinned(message, index, options) {
    var role = message && String(message.role || '').toLowerCase();
    if (PINNED_ROLES[role]) return true;
    return Array.isArray(options.pinnedIndexes) && options.pinnedIndexes.indexOf(index) >= 0;
  }

  function messageKey(message) {
    return String(message && message.role || '') + '\u0000' + textOf(message);
  }

  function dedupePinned(messages, options) {
    var seen = {};
    return messages.filter(function (message, index) {
      if (!isPinned(message, index, options)) return true;
      var key = messageKey(message);
      if (seen[key]) return false;
      seen[key] = true;
      return true;
    });
  }

  function clipText(value, maxChars) {
    var text = String(value == null ? '' : value);
    if (text.length <= maxChars) return text;
    var marker = ' ...[trimmed]';
    if (maxChars <= marker.length) return text.slice(0, Math.max(0, maxChars));
    return text.slice(0, maxChars - marker.length) + marker;
  }

  function summarySections(messages, carriedSummary, maxChars) {
    var action = [];
    var corrections = [];
    var unresolved = [];
    var conversation = [];
    var prior = String(carriedSummary || '').trim();
    if (prior) conversation.push('Prior summary: ' + clipText(prior, 600));

    (messages || []).forEach(function (message) {
      var text = textOf(message).replace(/\s+/g, ' ').trim();
      if (!text) return;
      var line = (message.role || 'message') + ': ' + clipText(text, 220);
      conversation.push(line);
      if (ACTION_RE.test(text)) action.push(line);
      if (CORRECTION_RE.test(text)) corrections.push(line);
      if (UNRESOLVED_RE.test(text)) unresolved.push(line);
    });

    var sections = [];
    if (conversation.length) sections.push('Earlier conversation:\n' + conversation.join('\n'));
    if (action.length) sections.push('Action outcomes:\n' + action.join('\n'));
    if (corrections.length) sections.push('Corrections:\n' + corrections.join('\n'));
    if (unresolved.length) sections.push('Open task state:\n' + unresolved.join('\n'));
    return clipText(sections.join('\n\n'), maxChars);
  }

  function component(chars, tokens, count) {
    return { chars: chars, tokens: tokens, messages: count };
  }

  function compactMessages(input, options) {
    options = options || {};
    var budget = Math.max(256, Number(options.budget) || DEFAULT_BUDGET);
    var recentTurns = Math.max(1, Number(options.recentTurns) || DEFAULT_RECENT_TURNS);
    var raw = Array.isArray(input) ? input.map(cloneMessage) : [];
    var messages = dedupePinned(raw, options);
    var pinned = [];
    var movable = [];
    var carriedSummary = '';

    messages.forEach(function (message, index) {
      var role = String(message.role || '').toLowerCase();
      if (role === 'summary' || (role === 'system' && /^\[conversation summary\]/i.test(textOf(message)))) {
        carriedSummary += (carriedSummary ? '\n' : '') + textOf(message);
        return;
      }
      if (isPinned(message, index, options)) pinned.push(message);
      else movable.push(message);
    });

    var recentCount = recentTurns * 2;
    var recent = movable.slice(-recentCount);
    var dropped = movable.slice(0, Math.max(0, movable.length - recent.length));
    var summary = summarySections(dropped, carriedSummary, Number(options.summaryChars) || 1800);
    var output = pinned.slice();
    var summaryMessage = null;
    if (summary) {
      summaryMessage = {
        role: options.summaryRole || 'system',
        content: '[Conversation Summary]\n' + summary
      };
      output.push(summaryMessage);
    }

    var pinnedTokens = pinned.reduce(function (sum, message) { return sum + estimateTokens(textOf(message)); }, 0);
    var summaryTokens = summaryMessage ? estimateTokens(summaryMessage.content) : 0;
    var remaining = Math.max(0, budget - pinnedTokens - summaryTokens);
    var fittedRecent = recent.map(cloneMessage);
    var recentTokens = fittedRecent.reduce(function (sum, message) { return sum + estimateTokens(textOf(message)); }, 0);

    // Keep the newest turns first when a request is unusually large. The full
    // history remains available in storage for the next turn and for sessions.
    while (fittedRecent.length > 1 && recentTokens > remaining) {
      fittedRecent.shift();
      recentTokens = fittedRecent.reduce(function (sum, message) { return sum + estimateTokens(textOf(message)); }, 0);
    }
    if (recentTokens > remaining && fittedRecent.length) {
      var perMessageChars = Math.max(24, Math.floor((remaining * 4) / fittedRecent.length));
      fittedRecent = fittedRecent.map(function (message) {
        var clipped = cloneMessage(message);
        if (typeof clipped.content === 'string') clipped.content = clipText(clipped.content, perMessageChars);
        else if (Array.isArray(clipped.parts)) clipped.parts = [{ text: clipText(textOf(clipped), perMessageChars) }];
        else clipped.content = clipText(textOf(clipped), perMessageChars);
        return clipped;
      });
    }
    output = output.concat(fittedRecent);

    var componentChars = { pinned: 0, summary: 0, recent: 0, actions: 0, corrections: 0, unresolved: 0 };
    var componentTokens = { pinned: 0, summary: 0, recent: 0, actions: 0, corrections: 0, unresolved: 0 };
    var componentMessages = { pinned: 0, summary: 0, recent: 0, actions: 0, corrections: 0, unresolved: 0 };
    output.forEach(function (message) {
      var text = textOf(message);
      var role = String(message.role || '').toLowerCase();
      var bucket = (role === 'summary' || /^\[conversation summary\]/i.test(text))
        ? 'summary' : (PINNED_ROLES[role] ? 'pinned' : 'recent');
      var tokens = estimateTokens(text);
      componentChars[bucket] += text.length;
      componentTokens[bucket] += tokens;
      componentMessages[bucket] += 1;
      if (bucket === 'summary') {
        if (ACTION_RE.test(text)) { componentChars.actions += text.length; componentTokens.actions += tokens; }
        if (CORRECTION_RE.test(text)) { componentChars.corrections += text.length; componentTokens.corrections += tokens; }
        if (UNRESOLVED_RE.test(text)) { componentChars.unresolved += text.length; componentTokens.unresolved += tokens; }
      }
    });

    var estimatedTokens = output.reduce(function (sum, message) { return sum + estimateTokens(textOf(message)); }, 0);
    return {
      messages: output,
      summary: summary,
      droppedMessages: dropped.length,
      dedupedMessages: raw.length - messages.length,
      estimatedTokens: estimatedTokens,
      inputMessages: raw.length,
      outputMessages: output.length,
      components: {
        pinned: component(componentChars.pinned, componentTokens.pinned, componentMessages.pinned),
        summary: component(componentChars.summary, componentTokens.summary, componentMessages.summary),
        recent: component(componentChars.recent, componentTokens.recent, componentMessages.recent),
        actions: component(componentChars.actions, componentTokens.actions, 0),
        corrections: component(componentChars.corrections, componentTokens.corrections, 0),
        unresolved: component(componentChars.unresolved, componentTokens.unresolved, 0)
      }
    };
  }

  function compactGeminiContents(contents, options) {
    var canonical = (Array.isArray(contents) ? contents : []).map(function (entry) {
      return {
        role: entry && entry.role === 'model' ? 'assistant' : 'user',
        content: textOf(entry)
      };
    });
    var packed = compactMessages(canonical, options);
    packed.messages = packed.messages.map(function (message) {
      return {
        role: message.role === 'assistant' ? 'model' : 'user',
        parts: [{ text: textOf(message) }]
      };
    });
    return packed;
  }

  function telemetryOf(packed) {
    var source = packed || {};
    return {
      estimatedTokens: source.estimatedTokens || 0,
      inputMessages: source.inputMessages || 0,
      outputMessages: source.outputMessages || 0,
      droppedMessages: source.droppedMessages || 0,
      dedupedMessages: source.dedupedMessages || 0,
      components: source.components || {}
    };
  }

  global.EvaPromptBudget = {
    DEFAULT_BUDGET: DEFAULT_BUDGET,
    estimateTokens: estimateTokens,
    textOf: textOf,
    compactMessages: compactMessages,
    compactGeminiContents: compactGeminiContents,
    telemetry: telemetryOf
  };
}(typeof window !== 'undefined' ? window : this));
