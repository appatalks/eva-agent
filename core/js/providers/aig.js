// aig.js
// Eva AIG (Artificial Intelligence Gateway) — Intelligent orchestration
// Routes through the bridge which picks the best model for each task,
// maintains Eva's persona, and handles data retrieval seamlessly.

var _aigLmStudioHealth = { baseUrl: '', checkedAt: 0, available: false };

function readAigQuestionInput(element) {
  return String(element && (element.innerText || element.textContent) || '').replace(/\u00a0/g, ' ');
}

async function saveExplicitFactsMemory(bridgeUrl, userMessage, sessionId, turnId) {
  var response = await fetch(bridgeUrl.replace(/\/+$/, '') + '/v1/memory/remember-facts', {
    method: 'POST',
    headers: (typeof getBridgeCapabilityHeaders === 'function') ? getBridgeCapabilityHeaders() : { 'Content-Type': 'application/json' },
    body: JSON.stringify({ user_message: userMessage, session_id: sessionId, turn_id: turnId }),
    signal: AbortSignal.timeout(5000)
  });
  var body = await response.json().catch(function () { return {}; });
  if (!response.ok) throw new Error((body.error && body.error.message) || ('HTTP ' + response.status));
  return body;
}

function committedFactSummary(facts) {
  var summaries = (facts || []).map(function(fact) {
    var relation = String(fact && fact.relation || '');
    var value = String(fact && fact.value || '').trim();
    if (relation === 'correct_spelling') return 'corrected spelling: ' + value;
    if (relation === 'user_children') return 'family: ' + value;
    if (relation === 'user_partner_name') return 'partner: ' + value;
    if (relation === 'user_name') return 'name: ' + value;
    return relation.replace(/^user_/, '').replace(/_/g, ' ') + ': ' + value;
  }).filter(Boolean);
  return summaries.length ? summaries.join('; ') : 'your explicit facts';
}

function briefingItems(summary, limit) {
  var items = [];
  var current = null;
  String(summary || '').split('\n').forEach(function (line) {
    var heading = line.match(/^\s*-\s+(.+?)\s*$/);
    if (heading) {
      if (current) items.push(current);
      current = { title: heading[1], detail: '' };
      return;
    }
    if (!current || !line.trim() || /^\s*https?:\/\//i.test(line)) return;
    if (!current.detail) current.detail = line.trim();
  });
  if (current) items.push(current);
  return items.slice(0, limit || 5);
}

function formatBriefingSection(summary, limit) {
  var items = briefingItems(summary, limit);
  if (!items.length) return String(summary || '').trim();
  return items.map(function (item) {
    return '- **' + item.title + '**' + (item.detail ? '\n  ' + item.detail : '');
  }).join('\n');
}

function requestedStockSymbol(text) {
  var value = String(text || '');
  var stopwords = {
    A: true, AN: true, AND: true, ARE: true, AT: true, CURRENT: true, FOR: true,
    HOW: true, IS: true, LAST: true, MARKET: true, ME: true, MY: true, NOT: true,
    OF: true, ON: true, PRICE: true, QUOTE: true, SHARE: true, STOCK: true,
    SYMBOL: true, THE: true, THEIR: true, TICKER: true, TODAY: true, WHAT: true
  };
  function normalize(candidate) {
    var symbol = String(candidate || '').toUpperCase();
    return /^[A-Z][A-Z0-9.-]{0,14}$/.test(symbol) && !stopwords[symbol] ? symbol : '';
  }
  var dollar = value.match(/\$([A-Za-z][A-Za-z0-9.-]{0,14})\b/);
  if (dollar) return normalize(dollar[1]);
  var qualified = value.match(/\b([A-Za-z][A-Za-z0-9.-]{0,14})\s*:\s*(?:AMEX|NYSEAMERICAN|NASDAQ|NYSE|OTC|OTCMKTS)\b/i);
  if (qualified) return normalize(qualified[1]);
  var afterSubject = value.match(/\b(?:stock|share|ticker|quote|price)(?:\s+(?:price|symbol))?\s+(?:of|for)\s+([A-Za-z][A-Za-z0-9.-]{0,14})\b/i);
  if (afterSubject) return normalize(afterSubject[1]);
  if (!/\b(?:stock|share|ticker|quote|price)\b/i.test(value)) return '';
  var uppercaseCandidates = (value.match(/\b[A-Z][A-Z0-9.-]{0,14}\b/g) || []).map(normalize).filter(Boolean);
  var uniqueCandidates = uppercaseCandidates.filter(function (candidate, index) {
    return uppercaseCandidates.indexOf(candidate) === index;
  });
  if (uniqueCandidates.length === 1) return uniqueCandidates[0];
  return '';
}

function contextualStockSymbol(text, messages) {
  var symbol = requestedStockSymbol(text);
  if (symbol) return symbol;
  var value = String(text || '');
  var quoteFollowUp = /\b(?:stock|share|ticker|quote|price)\b/i.test(value)
    && /\b(?:their|its|that\s+(?:company|stock)|the\s+(?:company|stock))\b/i.test(value);
  if (!quoteFollowUp) return '';
  for (var index = (messages || []).length - 1; index >= 0; index--) {
    var message = messages[index] || {};
    if (message.role !== 'user' && message.role !== 'assistant') continue;
    var content = typeof message.content === 'string' ? message.content : '';
    var receiptSymbol = content.match(/### Requested quote[\s\S]{0,160}?\*\*([A-Z][A-Z0-9.-]{0,14})(?:\s|·|\*)/);
    symbol = requestedStockSymbol(content) || (receiptSymbol && receiptSymbol[1]);
    if (symbol) return symbol;
  }
  return '';
}

function pendingEmailCommand(text) {
  var normalized = String(text || '').toLowerCase().replace(/[^a-z\s]/g, ' ').replace(/\s+/g, ' ').trim();
  if (/^(?:cancel|cancel it|cancel the email|do not send|don t send)$/.test(normalized)) return 'cancel';
  if (/^(?:confirm|confirmed|confrimed|approved|yes|yes send it|yes please send|please send|send it|send the email|send the message|confirmed please send|confirmed please continue|confrimed please send|confrimed please continue)$/.test(normalized)) return 'confirm';
  return '';
}

function explicitPendingEmailCommand(text, command) {
  var normalized = String(text || '').toLowerCase().replace(/[^a-z\s]/g, ' ').replace(/\s+/g, ' ').trim();
  if (command === 'cancel') return /^(?:cancel the email|do not send|don t send)$/.test(normalized);
  return /^(?:confirmed please send|confirmed please continue|confrimed|confrimed please send|confrimed please continue|please send|send it|send the email|send the message|yes please send|yes send it)$/.test(normalized);
}

function pendingEmailResultContent(result, command) {
  var decision = String(result && result.decision || 'failed');
  var transport = result && result.transport_status || {};
  if (decision === 'cancelled') return 'The pending email was cancelled. Nothing was sent.';
  if (decision === 'sent') return result.idempotent_replay
    ? 'That email was already sent. I did not send a duplicate.'
    : 'The email was sent.';
  if (decision === 'submitted') {
    if (transport.status === 'failed') return 'The local mail system accepted the email, but Exim could not deliver it: ' + String(transport.detail || 'transport failed') + ' The recipient did not receive it.';
    if (transport.status === 'deferred') return 'The local mail system accepted the email, but Exim deferred delivery and will retry: ' + String(transport.detail || 'delivery is deferred');
    if (transport.status === 'delivered') return 'Exim handed the email to its next SMTP hop. Final inbox delivery is not verified.';
    if (transport.status === 'pending') return 'The local mail system accepted the email and Exim is still processing it. Final delivery is not yet verified.';
    return result.idempotent_replay
      ? 'That email was already submitted to the local mail system. I did not submit a duplicate.'
      : 'The email was submitted to the local mail system. Transport status is not yet available.';
  }
  if (decision === 'partially_sent') return 'The email was delivered to some recipients only. I did not retry the completed submission.';
  if (decision === 'in_progress') return 'That email submission is already in progress.';
  return command === 'cancel'
    ? 'There is no pending email to cancel.'
    : 'The pending email could not be sent: ' + String(result && result.reason || 'it is missing or expired') + '.';
}

function requestedTestEmailDraft(text) {
  var value = String(text || '');
  if (/\b(?:do not|don'?t|never|without|unless|wait|cancel)\b[^.!?]{0,80}\b(?:send|sending|email|e-mail|mail)\b/i.test(value)) return null;
  var requestsTestEmail = /\b(?:send|sending|try\s+sending)\b[^.!?]{0,80}\btest\s+(?:email|e-mail)\b|\btest\s+(?:email|e-mail)\b[^.!?]{0,80}\b(?:send|sending)\b/i.test(value);
  if (!requestsTestEmail) return null;
  var addresses = value.match(/[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@(?:localhost|[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+)/gi) || [];
  var unique = addresses.map(function(address) { return address.toLowerCase(); }).filter(function(address, index, all) {
    return all.indexOf(address) === index;
  });
  if (unique.length !== 1) return null;
  return { to: unique[0], subject: 'Test email', body: 'This is a test email from Eva.' };
}

function contextualTestEmailDraft(text, messages) {
  var direct = requestedTestEmailDraft(text);
  if (direct) return direct;
  var value = String(text || '');
  if (/\b(?:do not|don'?t|never|without|unless|wait|cancel)\b[^.!?]{0,80}\b(?:send|sending|test)\b/i.test(value)) return null;
  var currentAddresses = value.match(/[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@(?:localhost|[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+)/gi) || [];
  var currentRecipients = currentAddresses.map(function(address) { return address.toLowerCase(); }).filter(function(address, index, all) {
    return all.indexOf(address) === index;
  });
  var explicitTestContinuation = /\b(?:send|sending)\b[^.!?]{0,80}\btest\b|\btest\b[^.!?]{0,80}\b(?:send|sending)\b/i.test(value);
  var recipientRevision = currentRecipients.length === 1
    && /\b(?:send|sending|deliver|delivering|use)\b[^.!?]{0,80}@/i.test(value);
  if (!explicitTestContinuation && !recipientRevision) return null;
  var addresses = [];
  var priorUsers = 0;
  var hasPriorTestEmail = false;
  for (var index = (messages || []).length - 1; index >= 0 && priorUsers < 6; index--) {
    var message = messages[index] || {};
    if (message.role !== 'user' || typeof message.content !== 'string' || message.content === value) continue;
    priorUsers += 1;
    if (!/\b(?:email|e-mail|mail)\b/i.test(message.content)) continue;
    if (/\btest\s+(?:email|e-mail)\b/i.test(message.content)) hasPriorTestEmail = true;
    var found = message.content.match(/[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@(?:localhost|[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+)/gi) || [];
    found.forEach(function(address) {
      address = address.toLowerCase();
      if (addresses.indexOf(address) < 0) addresses.push(address);
    });
  }
  if (recipientRevision) {
    if (!hasPriorTestEmail) return null;
    return { to: currentRecipients[0], subject: 'Test email', body: 'This is a test email from Eva.' };
  }
  if (addresses.length !== 1) return null;
  return { to: addresses[0], subject: 'Test email', body: 'This is a test email from Eva.' };
}

function formatBriefingQuote(quote, requestedSymbol) {
  quote = quote || {};
  var symbol = String(quote.symbol || requestedSymbol || 'Requested ticker').toUpperCase();
  if (!quote || typeof quote.price !== 'number') {
    return '### Requested quote\n\n_Verified current price for ' + symbol + ' is unavailable from the configured local quote source._';
  }
  var currency = String(quote.currency || '').trim();
  var exchange = String(quote.exchange || '').trim();
  var lines = ['### Requested quote', '- **' + symbol + (exchange ? ' · ' + exchange : '') + '**: ' + (currency ? currency + ' ' : '') + quote.price];
  if (typeof quote.change === 'number' && typeof quote.change_percent === 'number') {
    var sign = quote.change > 0 ? '+' : '';
    lines.push('- Session move: ' + sign + quote.change + ' (' + sign + quote.change_percent + '%)');
    lines.push('- Analysis: The verified receipt shows a ' + (quote.change > 0 ? 'move above' : quote.change < 0 ? 'move below' : 'move in line with') + ' the previous close. It does not include enough historical or fundamental data for a broader assessment.');
  } else {
    lines.push('- Analysis: The verified receipt does not include a prior-close comparison or broader historical data.');
  }
  return lines.join('\n');
}

async function fetchBriefingQuote(bridgeUrl, userMessage, sessionId, resolvedSymbol) {
  var symbol = resolvedSymbol || requestedStockSymbol(userMessage);
  if (!symbol) return { content: '', available: false };
  try {
    var quoteRequest = '$' + symbol + ' stock quote';
    var url = bridgeUrl.replace(/\/+$/, '') + '/v1/data/retrieve?message=' + encodeURIComponent(quoteRequest) + '&session_id=' + encodeURIComponent(sessionId || '');
    var response = await fetch(url, {
      headers: (typeof getBridgeCapabilityHeaders === 'function') ? getBridgeCapabilityHeaders() : {},
      signal: AbortSignal.timeout(25000)
    });
    var body = await response.json().catch(function () { return {}; });
    var receipt = {};
    try { receipt = JSON.parse(String(body.data || '')); } catch (_) {}
    var quote = receipt.stock_quote || {};
    return { content: formatBriefingQuote(quote, symbol), available: typeof quote.price === 'number' };
  } catch (_) {
    return { content: formatBriefingQuote({}, symbol), available: false };
  }
}

function formatPreparedBriefing(status, preparing, requestedQuote) {
  status = status || {};
  var sources = status.sources || {};
  var lines = ['## Morning briefing'];
  var unavailable = [];
  var rendered = 0;
  var weather = sources.weather || {};
  var weatherSummary = String(weather.summary || '').trim();
  if (weather.status === 'ready' && weatherSummary) {
    lines.push('### Weather', formatBriefingSection(weatherSummary, 2));
    rendered += 1;
  } else if (!preparing && weatherSummary) {
    unavailable.push('weather');
  }

  var mail = sources.mail || {};
  var memory = sources.memory || {};
  var dayContext = [mail, memory].map(function (source) {
    return source.status === 'ready' || source.status === 'partial' ? String(source.summary || '').trim() : '';
  }).filter(Boolean).join('\n');
  if (dayContext) {
    lines.push('### Your day', dayContext);
    rendered += 1;
  }

  var news = sources.news || {};
  if (news.status === 'ready' && news.summary) {
    lines.push('### Headlines', formatBriefingSection(news.summary, 5));
    rendered += 1;
  } else if (!preparing && news.summary) {
    unavailable.push('headlines');
  }

  var markets = sources.markets || {};
  if (markets.status === 'ready' && markets.summary) {
    lines.push('### Market news', '_Latest eligible coverage may describe the most recently completed U.S. trading session._\n\n' + formatBriefingSection(markets.summary, 3));
    rendered += 1;
  } else if (!preparing && markets.summary) {
    unavailable.push('market news');
  }
  if (requestedQuote) {
    lines.push(requestedQuote);
    rendered += 1;
  }
  if (preparing) lines.push('_Gathering the remaining live sections..._');
  if (unavailable.length) lines.push('_Live sources unavailable: ' + unavailable.join(', ') + '._');
  if (!rendered && !preparing) lines.push('Live briefing data is unavailable right now.');
  return lines.join('\n\n');
}

function updatePreparedBriefingPreview(preview, status, txtOutput) {
  if (!preview || !preview.body) return;
  preview.body.innerHTML = renderMarkdown(formatPreparedBriefing(status, true));
  txtOutput.scrollTop = txtOutput.scrollHeight;
}

async function waitForPreparedBriefing(bridgeUrl, initialStatus, onProgress) {
  var latest = initialStatus || {};
  if (latest.status !== 'preparing') return latest;
  var deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    await new Promise(function (resolve) { setTimeout(resolve, 750); });
    try {
      var response = await fetch(bridgeUrl.replace(/\/+$/, '') + '/v1/briefing/status', {
        signal: AbortSignal.timeout(1200)
      });
      if (!response.ok) continue;
      latest = await response.json();
      if (typeof onProgress === 'function') onProgress(latest);
      if (latest.status !== 'preparing') return latest;
      setStatus('info', 'Eva is still preparing the morning briefing...');
    } catch (_) {}
  }
  return latest;
}

function aigLmStudioAvailable() {
  var baseUrl = (typeof getLmStudioBaseUrl === 'function' ? getLmStudioBaseUrl() : '').replace(/\/+$/, '');
  var now = Date.now();
  if (!baseUrl) return false;
  if (_aigLmStudioHealth.baseUrl !== baseUrl || now - _aigLmStudioHealth.checkedAt > 30000) {
    _aigLmStudioHealth.baseUrl = baseUrl;
    _aigLmStudioHealth.checkedAt = now;
    _aigLmStudioHealth.available = false;
    fetch(baseUrl + '/models', { signal: AbortSignal.timeout(1500) }).then(function(response) {
      _aigLmStudioHealth.available = response.ok;
    }).catch(function() {});
  }
  return _aigLmStudioHealth.available;
}

async function aigSend() {
  var txtMsg = document.getElementById('txtMsg');
  var txtOutput = document.getElementById('txtOutput');
  var pendingImageData = window._evaPendingImageData || '';
  var imageMatch = pendingImageData.match(/^data:(image\/(?:jpeg|png|webp|gif));base64,/);
  var imageB64 = imageMatch ? pendingImageData.slice(imageMatch[0].length) : '';
  var imageMime = imageMatch ? imageMatch[1] : 'image/jpeg';

  var sQuestion = readAigQuestionInput(txtMsg);
  if (!sQuestion.trim()) {
    alert('Type in your question!');
    txtMsg.focus();
    return;
  }
  window._evaPendingImageData = '';
  var signalContext = (typeof captureSignalDeliveryContext === 'function')
    ? captureSignalDeliveryContext(sQuestion)
    : null;
  var sessionId = (typeof ensureActiveSessionId === 'function')
    ? ensureActiveSessionId() : ((typeof _activeSessionId === 'function') ? (_activeSessionId() || '') : '');
  var turnId = window._evaActiveAuditTurnId || ((typeof EvaRequestRouting !== 'undefined' && EvaRequestRouting.createTurnId) ? EvaRequestRouting.createTurnId() : '');
  var briefingRequest = /\b(?:morning|daily)\s+(?:briefing|report|update)\b/i.test(sQuestion);

  // Display user message
  var safeUser = escapeHtml(sQuestion).replace(/\n/g, '<br>');
  txtOutput.innerHTML += '<div class="chat-bubble user-bubble"><span class="user">You:</span> ' + safeUser + '</div>';
  txtMsg.innerHTML = '';
  txtOutput.scrollTop = txtOutput.scrollHeight;

  // Build messages payload
  var storageKey = 'aigMessages';
  if (!localStorage.getItem(storageKey)) {
    var sysPrompt = (typeof getSystemPrompt === 'function') ? getSystemPrompt() : '';
    var initMessages = [
      { role: 'system', content: sysPrompt + ' When you are asked to show an image, instead describe the image with [Image of <Description>]. ' + (typeof dateContents !== 'undefined' ? dateContents : '') }
    ];
    localStorage.setItem(storageKey, JSON.stringify(initMessages));
  }

  var newMessages = [];
  if (lastResponse) {
    newMessages.push({ role: 'assistant', content: lastResponse.replace(/\n/g, ' ') });
  }
  newMessages.push(imageB64 ? {
    role: 'user',
    content: [
      { type: 'text', text: sQuestion },
      { type: 'image_url', image_url: { url: pendingImageData } }
    ]
  } : { role: 'user', content: sQuestion });

  // External data augmentation
  if (sQuestion.includes('weather') && typeof weatherContents !== 'undefined' && weatherContents) {
    newMessages.push({ role: 'user', content: "Today's " + weatherContents + ". " + sQuestion });
  }
  if (sQuestion.includes('news') && typeof newsContents !== 'undefined' && newsContents) {
    newMessages.push({ role: 'user', content: "Today's " + newsContents + ". " + sQuestion });
  }
  if ((sQuestion.includes('stock') || sQuestion.includes('markets') || sQuestion.includes('SPY')) && typeof marketContents !== 'undefined' && marketContents) {
    newMessages.push({ role: 'user', content: "Today's " + marketContents + " " + sQuestion });
  }
  if ((sQuestion.includes('solar') || sQuestion.includes('space weather')) && typeof solarContents !== 'undefined' && solarContents) {
    newMessages.push({ role: 'user', content: "Today's " + solarContents + " " + sQuestion });
  }

  var existingMessages = JSON.parse(localStorage.getItem(storageKey)) || [];
  var requestMessages = existingMessages.concat(newMessages);
  var currentHarnessContract = (window.EvaHarness && typeof EvaHarness.promptContract === 'function')
    ? EvaHarness.promptContract() : '';
  var hasEmailHarnessContract = requestMessages.some(function(message) {
    return (message.role === 'system' || message.role === 'developer')
      && String(message.content || '').indexOf('prepare_email') >= 0;
  });
  if (currentHarnessContract && !hasEmailHarnessContract) {
    requestMessages.push({ role: 'system', content: currentHarnessContract });
  }
  var storedMessages = newMessages.map(function (message) {
    if (!Array.isArray(message.content)) return message;
    var text = message.content.filter(function (part) {
      return part && part.type === 'text';
    }).map(function (part) { return part.text || ''; }).join('\n').trim();
    return { role: message.role, content: text || '[Image attachment]' };
  });
  existingMessages = existingMessages.concat(storedMessages);
  localStorage.setItem(storageKey, JSON.stringify(existingMessages));

  var workspaceMcpRequest = /\b(?:mcp|workspace\s+(?:module|server|tool)|work\s*iq)\b/i.test(sQuestion);
  if (workspaceMcpRequest && window.EvaWorkspaces && typeof EvaWorkspaces.mcpContext === 'function') {
    var workspaceMcpContext = EvaWorkspaces.mcpContext();
    if (workspaceMcpContext) requestMessages.push({ role: 'system', content: workspaceMcpContext });
  }

  var researchPlan = (window.EvaRequestRouting && typeof EvaRequestRouting.resolveResearchRequest === 'function')
    ? EvaRequestRouting.resolveResearchRequest(sQuestion, requestMessages)
    : { active: false, query: '', strategy: 'search', needs_topic: false, continuation: false };
  var researchHistory = (window.EvaRequestRouting && typeof EvaRequestRouting.getResearchHistory === 'function')
    ? EvaRequestRouting.getResearchHistory(requestMessages)
    : requestMessages.filter(function (message) { return message && message.role === 'user' && typeof message.content === 'string'; })
      .map(function (message) { return message.content; }).slice(-6);
  var nativeResearch = !!researchPlan.active;

  // Send to AIG orchestrator via bridge
  var bridgeUrl = (typeof getACPBridgeUrl === 'function') ? getACPBridgeUrl() : 'http://localhost:8888';
  if (typeof watchACPPermissions === 'function') watchACPPermissions(190000);

  var emailCommand = pendingEmailCommand(sQuestion);
  if (emailCommand && window.EvaEmailSettings && typeof EvaEmailSettings.hasPending === 'function'
      && (EvaEmailSettings.hasPending(sessionId) || explicitPendingEmailCommand(sQuestion, emailCommand))) {
    setStatus('info', emailCommand === 'confirm' ? 'Eva is submitting the approved email...' : 'Eva is cancelling the pending email...');
    var pendingEmailResult;
    try {
      pendingEmailResult = emailCommand === 'confirm'
        ? await EvaEmailSettings.confirmPending(sessionId)
        : await EvaEmailSettings.cancelPending(sessionId);
    } catch (emailError) {
      pendingEmailResult = { decision: 'failed', reason: emailError && emailError.message };
    }
    var pendingEmailContent = pendingEmailResultContent(pendingEmailResult, emailCommand);
    await renderEvaResponse(pendingEmailContent, txtOutput, {
      nativeRequest: sQuestion,
      nativeResearch: nativeResearch,
      turnId: turnId
    });
    existingMessages.push({ role: 'assistant', content: pendingEmailContent });
    localStorage.setItem(storageKey, JSON.stringify(existingMessages));
    lastResponse = pendingEmailContent;
    masterOutput += txtOutput.innerText + '\n';
    localStorage.setItem('masterOutput', masterOutput);
    var emailSucceeded = ['sent', 'submitted', 'partially_sent'].indexOf(String(pendingEmailResult && pendingEmailResult.decision || '')) >= 0;
    setStatus(emailSucceeded || emailCommand === 'cancel' ? 'info' : 'warn', pendingEmailContent);
    var emailAutoSpeak = document.getElementById('autoSpeak');
    if (emailAutoSpeak && emailAutoSpeak.checked) speakText();
    return;
  }

  var testEmailDraft = contextualTestEmailDraft(sQuestion, existingMessages);
  if (testEmailDraft && window.EvaEmailSettings && typeof EvaEmailSettings.prepare === 'function') {
    setStatus('info', 'Eva is preparing the email for confirmation...');
    var preparedEmail;
    try {
      preparedEmail = await EvaEmailSettings.prepare(testEmailDraft, sessionId);
    } catch (prepareError) {
      preparedEmail = { decision: 'rejected', reason: prepareError && prepareError.message };
    }
    var preparedEmailContent;
    if (preparedEmail && preparedEmail.decision === 'pending_confirmation') {
      preparedEmailContent = 'I prepared this email for **' + testEmailDraft.to + '**:\n\n'
        + '- **Subject:** ' + testEmailDraft.subject + '\n'
        + '- **Body:** ' + testEmailDraft.body + '\n\n'
        + 'Confirm this exact message to send it, or cancel it.';
    } else {
      preparedEmailContent = 'I could not prepare the email: '
        + String(preparedEmail && preparedEmail.reason || 'no connected sending account is available') + '.';
    }
    await renderEvaResponse(preparedEmailContent, txtOutput, { nativeRequest: sQuestion, nativeResearch: nativeResearch, turnId: turnId });
    existingMessages.push({ role: 'assistant', content: preparedEmailContent });
    localStorage.setItem(storageKey, JSON.stringify(existingMessages));
    lastResponse = preparedEmailContent;
    masterOutput += txtOutput.innerText + '\n';
    localStorage.setItem('masterOutput', masterOutput);
    setStatus(preparedEmail && preparedEmail.decision === 'pending_confirmation' ? 'info' : 'warn', preparedEmailContent);
    return;
  }

  var savedFacts;
  try {
    savedFacts = await saveExplicitFactsMemory(bridgeUrl, sQuestion, sessionId, turnId);
  } catch (memoryError) {
    requestMessages.push({
      role: 'system',
      content: 'Durable-memory preflight was unavailable for this turn. Do not claim that any new fact was saved.'
    });
    setStatus('warn', 'Eva could not save explicit facts to durable memory.');
  }
  if (savedFacts && savedFacts.status === 'saved') {
    var savedFactsReceipt = 'Durable-memory commit succeeded for this turn: '
      + committedFactSummary(savedFacts.facts)
      + '. Briefly acknowledge the saved facts, then answer any other request in the user message. Do not claim that any other facts were saved.';
    requestMessages.push({ role: 'system', content: savedFactsReceipt });
    if (typeof evaAuditEvent === 'function') {
      evaAuditEvent('native_action', 'completed', {
        correlation_id: turnId,
        action: 'remember_facts',
        label: 'Durable Memory'
      });
    }
    setStatus('info', 'Eva saved explicit facts to durable memory.');
  }

  setStatus('info', 'Eva (AIG) processing...');
  // Optional cognitive layer (eva / reviewer).
  // Runs when the Settings toggle is on OR the user message contains an
  // explicit trigger phrase like "trigger the chain" / "use cognition".
  // Falls back to the regular single-shot bridge call on any error.
  var cogDecision = (typeof Cognition !== 'undefined' && Cognition.shouldRun)
                      ? Cognition.shouldRun(sQuestion)
                      : { active: false, reason: null };
  var requestedQuoteSymbol = contextualStockSymbol(sQuestion, existingMessages);
  var isDirectQuoteRequest = !!requestedQuoteSymbol
    && /\b(?:stock|share|ticker|quote|price)\b/i.test(sQuestion)
    && !/\b(?:analy[sz]e|forecast|strategy|compare|valuation|fundamental|thesis)\b/i.test(sQuestion);
  if (isDirectQuoteRequest && cogDecision.reason !== 'phrase') {
    cogDecision = { active: false, reason: 'direct-quote' };
  }
  if (briefingRequest) {
    cogDecision = { active: false, reason: 'briefing-cache' };
    var briefingPreview = null;
    try {
      var briefingResponse = await fetch(bridgeUrl.replace(/\/+$/, '') + '/v1/briefing/refresh', {
        method: 'POST',
        headers: (typeof getBridgeCapabilityHeaders === 'function') ? getBridgeCapabilityHeaders() : { 'Content-Type': 'application/json' },
        signal: AbortSignal.timeout(1500)
      });
      if (!briefingResponse.ok) throw new Error('Briefing status unavailable');
      var briefingStatus = briefingResponse.ok ? await briefingResponse.json() : {};
      if (briefingStatus.status === 'preparing') {
        briefingPreview = document.createElement('div');
        briefingPreview.className = 'chat-bubble eva-bubble eva-briefing-preview';
        briefingPreview.innerHTML = '<span class="eva">Eva:</span> <div class="md"></div>';
        txtOutput.appendChild(briefingPreview);
        updatePreparedBriefingPreview({ body: briefingPreview.querySelector('.md') }, briefingStatus, txtOutput);
        setStatus('info', 'Eva is preparing the morning briefing...');
      }
      briefingStatus = await waitForPreparedBriefing(bridgeUrl, briefingStatus, function (latest) {
        if (briefingPreview) {
          updatePreparedBriefingPreview({ body: briefingPreview.querySelector('.md') }, latest, txtOutput);
        }
      });
      if (briefingPreview && briefingPreview.parentNode) briefingPreview.parentNode.removeChild(briefingPreview);
      var requestedQuote = await fetchBriefingQuote(bridgeUrl, sQuestion, sessionId);
      var briefingContent = formatPreparedBriefing(briefingStatus, briefingStatus.status === 'preparing', requestedQuote.content);
      await renderEvaResponse(briefingContent, txtOutput, {
        nativeRequest: sQuestion,
        nativeResearch: nativeResearch,
        turnId: turnId
      });
      existingMessages.push({ role: 'assistant', content: briefingContent });
      localStorage.setItem(storageKey, JSON.stringify(existingMessages));
      lastResponse = briefingContent;
      masterOutput += txtOutput.innerText + '\n';
      localStorage.setItem('masterOutput', masterOutput);
      setStatus('info', 'Eva morning briefing - prepared live sources');
      var briefingAutoSpeak = document.getElementById('autoSpeak');
      if (briefingAutoSpeak && briefingAutoSpeak.checked) speakText();
      return;
    } catch (briefingError) {
      if (briefingPreview && briefingPreview.parentNode) briefingPreview.parentNode.removeChild(briefingPreview);
      var briefingErrorContent = '## Morning briefing\n\nLive briefing data is unavailable right now. Please try again shortly.';
      await renderEvaResponse(briefingErrorContent, txtOutput, {
        nativeRequest: sQuestion,
        nativeResearch: nativeResearch,
        turnId: turnId
      });
      existingMessages.push({ role: 'assistant', content: briefingErrorContent });
      localStorage.setItem(storageKey, JSON.stringify(existingMessages));
      lastResponse = briefingErrorContent;
      masterOutput += txtOutput.innerText + '\n';
      localStorage.setItem('masterOutput', masterOutput);
      setStatus('warn', 'Eva could not retrieve the live morning briefing.');
      return;
    }
  }
  if (isDirectQuoteRequest) {
    var quotePreview = createEvaStreamingBubble(txtOutput);
    updateEvaStreamingStatus(quotePreview, {
      phase: 'thinking',
      text: 'Eva is retrieving live data...'
    }, txtOutput);
    var directQuote = await fetchBriefingQuote(bridgeUrl, sQuestion, sessionId, requestedQuoteSymbol);
    removeEvaStreamingBubble(quotePreview);
    await renderEvaResponse(directQuote.content, txtOutput, {
      nativeRequest: sQuestion,
      nativeResearch: nativeResearch,
      turnId: turnId
    });
    existingMessages.push({ role: 'assistant', content: directQuote.content });
    localStorage.setItem(storageKey, JSON.stringify(existingMessages));
    lastResponse = directQuote.content;
    masterOutput += txtOutput.innerText + '\n';
    localStorage.setItem('masterOutput', masterOutput);
    setStatus(directQuote.available ? 'info' : 'warn', directQuote.available
      ? 'Eva verified the current ' + requestedQuoteSymbol + ' quote.'
      : 'Eva could not verify the current ' + requestedQuoteSymbol + ' quote.');
    var directQuoteAutoSpeak = document.getElementById('autoSpeak');
    if (directQuoteAutoSpeak && directQuoteAutoSpeak.checked) speakText();
    return;
  }
  var provisional = createEvaStreamingBubble(txtOutput);
  updateEvaStreamingStatus(provisional, {
    phase: 'thinking',
    text: 'Eva is preparing context...'
  }, txtOutput);
  if (cogDecision.active) {
    if (cogDecision.reason === 'phrase') {
      setStatus('info', 'Eva cognition force-enabled by phrase trigger...');
    }
    var cognitionFinalizing = false;
    try {
      var cogResult = await Cognition.run({
        userMessage: sQuestion,
        messages: requestMessages,
        sessionId: sessionId,
        turnId: turnId,
        imageB64: imageB64,
        imageMime: imageMime,
        researchHistory: researchHistory,
        researchPlan: researchPlan,
        nativeResearch: nativeResearch,
        forceEnable: cogDecision.reason === 'phrase',
        forcedReason: cogDecision.reason,
        reviewReason: cogDecision.reason,
        onStatus: function (text) {
          if (!provisional) return;
          updateEvaStreamingStatus(provisional, {
            phase: 'thinking',
            text: text
          }, txtOutput);
        }
      });
      var cogContent = (cogResult && cogResult.content) ? cogResult.content : '';
      // Execute any [[EVA_ACTION]] blocks Eva emitted, then render.
      var actionsRun = [];
      if (Cognition.executeActions) {
        var execRes = await Cognition.executeActions(cogContent, {
          userMessage: sQuestion,
          nativeResearch: nativeResearch
        });
        cogContent = execRes.content;
        actionsRun = execRes.actions || [];
      }
      var deferredSignal = false;
      if (Cognition.ensureAgentLaunch) {
        var launchResult = await Cognition.ensureAgentLaunch({
          userMessage: sQuestion,
          content: cogContent,
          actions: actionsRun,
          nativeResearch: nativeResearch
        });
        cogContent = launchResult.content;
        actionsRun = launchResult.actions || actionsRun;
        deferredSignal = !!launchResult.deferredSignal;
      }
      cognitionFinalizing = true;
      removeEvaStreamingBubble(provisional);
      provisional = null;
      await renderEvaResponse(cogContent, txtOutput, {
        signalAuthorized: !deferredSignal && !!(signalContext && signalContext.authorized),
        signalMessage: deferredSignal ? '' : (signalContext ? signalContext.message : ''),
        signalRequest: sQuestion,
        nativeRequest: sQuestion,
        nativeResearch: nativeResearch,
        turnId: turnId,
        signalContext: signalContext
      });
      if (Cognition.getCfg && Cognition.getCfg().showTrace && Cognition.renderTraceHtml) {
        try {
          txtOutput.innerHTML += Cognition.renderTraceHtml(cogResult.trace || []);
          txtOutput.scrollTop = txtOutput.scrollHeight;
        } catch (_) {}
      }
      if (cogContent) {
        lastResponse = cogContent;
        masterOutput += txtOutput.innerText + '\n';
        localStorage.setItem('masterOutput', masterOutput);
      }
      var cogTag = 'cog:' + (cogResult.evaModel || '?') + '+' +
                   (cogResult.reviewerModel || '?') +
                   '/c' + (cogResult.cycles || 0) +
                   (cogDecision.reason === 'phrase' ? '/forced' : '') +
                   (actionsRun.length ? '/act' + actionsRun.length : '');
      if (cogContent) {
        try {
          fetch(bridgeUrl.replace(/\/+$/, '') + '/v1/memory/reflect', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              user_message: sQuestion,
              assistant_message: cogContent,
              model: cogTag,
              session_id: sessionId,
              turn_id: turnId
            }),
            signal: AbortSignal.timeout(5000)
          }).catch(function () {});
        } catch (_) {}
      }
      setStatus('info', 'Eva (AIG, cognition) \u2014 ' +
                (cogResult.evaModel || 'eva') +
                '  [' + cogTag + ']');
      var checkboxC = document.getElementById('autoSpeak');
      if (checkboxC && checkboxC.checked) {
        speakText();
        var audioC = document.getElementById('audioPlayback');
        if (audioC) audioC.setAttribute('autoplay', true);
      }
      return;
    } catch (cogErr) {
      var cogMsg = (cogErr && cogErr.message) ? cogErr.message : String(cogErr);
      if (cognitionFinalizing) {
        setStatus('error', 'Eva could not finalize the response: ' + cogMsg);
        return;
      }
      setStatus('warn', 'Cognition failed, falling back: ' + cogMsg);
      // fall through to single-shot path
    }
  } else {
    // Single-shot path: tell Eva that adaptive review was not selected for
    // this turn, so she neither invents a pipeline nor claims it is disabled.
    var cogState = (typeof Cognition !== 'undefined' && Cognition.getCfg)
                     ? Cognition.getCfg() : null;
    var cogNote = [
      '[Cognition Layer Runtime State - AUTHORITATIVE]',
      'Adaptive review did NOT run for this turn; the selected AIG backend is responding directly.',
      'Adaptive review is controlled by Settings > Models > "Adaptive Review" and activates for',
      'consequential work or an explicit phrase trigger such as "trigger the chain" or "use cognition".',
      'If asked whether review ran, answer truthfully: it did not run for this turn.',
      'Never narrate a fake pipeline (no PHASE 1 / PHASE 2 / PHASE 3 headers, no fabricated reviewer feedback).',
      'The .github/agents/*.agent.md files describe VS Code Copilot review agents and are NOT your runtime tools.',
      'If the user wants the layer, tell them to enable the toggle or use a trigger phrase.'
    ].join('\n');
    if (cogState) {
      cogNote += '\nConfigured models when enabled: eva=' + cogState.evaModel +
                 ', reviewer=' + cogState.reviewerModel +
                 ', maxCycles=' + cogState.maxCycles + '.';
    }
    requestMessages = requestMessages.concat([{ role: 'system', content: cogNote }]);
    existingMessages = existingMessages.concat([{ role: 'system', content: cogNote }]);
    localStorage.setItem(storageKey, JSON.stringify(existingMessages));
  }

  try {
    var url = bridgeUrl.replace(/\/+$/, '') + '/v1/aig/chat';
    var aigPromptBudget = EvaPromptBudget.compactMessages(requestMessages, {
      budget: 12000,
      recentTurns: 6
    });

    // The selected AIG backend is Eva's primary model. Adaptive review uses a
    // separate reviewer model and must never override this direct responder.
    var aigModel = (document.getElementById('selAIGBackend') || {}).value || 'gpt-5.6-luna';
    var reasoningEffort = (typeof getReasoningEffortForModel === 'function') ? getReasoningEffortForModel('aig') : 'default';

    if (!provisional) {
      provisional = createEvaStreamingBubble(txtOutput);
      updateEvaStreamingStatus(provisional, {
        phase: 'thinking',
        text: 'Eva is preparing context...'
      }, txtOutput);
    }
    var resp = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        messages: aigPromptBudget.messages,
        prompt_budget: EvaPromptBudget.telemetry(aigPromptBudget),
        user_message: sQuestion,
        session_id: sessionId,
        turn_id: turnId,
        model: aigModel,
        model_policy_mode: (typeof getAIGModelPolicyMode === 'function') ? getAIGModelPolicyMode() : 'auto-balanced',
        max_completion_tokens: (typeof getModelMaxTokens === 'function') ? getModelMaxTokens() : 16384,
        acp_reasoning_effort: reasoningEffort === 'default' ? '' : reasoningEffort,
        lmstudio_base_url: (typeof getLmStudioBaseUrl === 'function') ? getLmStudioBaseUrl() : '',
        lmstudio_model: (typeof getLmStudioModel === 'function') ? getLmStudioModel() : '',
        image_b64: imageB64,
        image_mime: imageMime,
        research_history: researchHistory,
        lmstudio_available: aigLmStudioAvailable(),
        openai_api_key: (typeof getAuthKey === 'function') ? getAuthKey('OPENAI_API_KEY') : '',
        acp_auto_approve: true,
        stream: true
      })
    });

    if (!resp.ok) {
      removeEvaStreamingBubble(provisional);
      var errText = await resp.text();
      var errMsg = 'AIG Error ' + resp.status + ': ' + errText;
      txtOutput.innerHTML += '<div class="chat-bubble eva-bubble"><span class="error">' + escapeHtml(errMsg) + '</span></div>';
      txtOutput.scrollTop = txtOutput.scrollHeight;
      setStatus('error', errMsg);
      return;
    }

    var data = await readEvaStreamingResponse(resp, function (chunk) {
      if (!provisional) provisional = createEvaStreamingBubble(txtOutput);
      appendEvaStreamingChunk(provisional, chunk, txtOutput);
    }, function (event) {
      if (!provisional) provisional = createEvaStreamingBubble(txtOutput);
      updateEvaStreamingStatus(provisional, event, txtOutput);
    }, function (reasoning) {
      if (!provisional) provisional = createEvaStreamingBubble(txtOutput);
      appendEvaStreamingReasoning(provisional, reasoning, txtOutput);
    });
    removeEvaStreamingBubble(provisional);
    var content = (data.choices && data.choices[0] && data.choices[0].message && data.choices[0].message.content) || '';
    var responseMessage = (data.choices && data.choices[0] && data.choices[0].message) || {};
    var reasoningContent = responseMessage.reasoning_content || '';
    var modelUsed = data.model || 'aig';

    // Render response
    await renderEvaResponse(content, txtOutput, {
      signalAuthorized: !!(signalContext && signalContext.authorized),
      signalMessage: signalContext ? signalContext.message : '',
      signalRequest: sQuestion,
      nativeRequest: sQuestion,
      nativeResearch: nativeResearch,
      turnId: turnId,
      signalContext: signalContext,
      reasoningContent: reasoningContent
    });

    if (content) {
      lastResponse = content;
      var outputWithoutTags = txtOutput.innerText + '\n';
      masterOutput += outputWithoutTags;
      localStorage.setItem('masterOutput', masterOutput);
    }

    // Friendly status: pull the actual responder model out of the bridge tag
    // (e.g. "aig:gpt-5.5+copilot-acp" -> responder "gpt-5.5", route "via ACP").
    var responder = modelUsed;
    var routeLabel = '';
    var stripped = String(modelUsed).replace(/^aig:/, '');
    var firstSegment = stripped.split('+')[0] || stripped;
    if (firstSegment) responder = firstSegment;
    var acpTagRe = /(^|\+)(copilot-acp|acp-data|raw-acp|raw-acp-unavailable|acp-default)$/;
    if (/(^|\+)openai-direct($|\+)/.test(stripped)) {
      routeLabel = ' via OpenAI API';
    } else if (/^(claude-|gemini-)/.test(responder) || acpTagRe.test(stripped) || responder === 'acp-default') {
      routeLabel = ' via ACP';
    } else if (/^(gpt-|o\d|deepseek-|llama-)/.test(responder)) {
      routeLabel = ' via AIG';
    }
    if (responder === 'unavailable' || responder === 'raw-acp-unavailable') {
      setStatus('error', 'Eva (AIG) responder unavailable (' + modelUsed + ')');
    } else if (typeof reportCompletionTruncation === 'function' && reportCompletionTruncation(data)) {
    } else {
      setStatus('info', 'Eva (AIG) \u2014 ' + responder + routeLabel + '  [' + modelUsed + ']');
    }

    // Auto-speak
    var checkbox = document.getElementById('autoSpeak');
    if (checkbox && checkbox.checked) {
      speakText();
      var audio = document.getElementById('audioPlayback');
      if (audio) audio.setAttribute('autoplay', true);
    }

  } catch (err) {
    removeEvaStreamingBubble(typeof provisional === 'undefined' ? null : provisional);
    var errorMessage = err.message || String(err);
    if (errorMessage.includes('Failed to fetch') || errorMessage.includes('NetworkError')) {
      errorMessage += ' — Is the ACP bridge server running? Start it with: python3 tools/acp_bridge.py --enable-kusto-mcp';
    }
    txtOutput.innerHTML += '<div class="chat-bubble eva-bubble"><span class="error">AIG Error:</span> ' + escapeHtml(errorMessage) + '</div>';
    txtOutput.scrollTop = txtOutput.scrollHeight;
    setStatus('error', errorMessage);
  }
}
