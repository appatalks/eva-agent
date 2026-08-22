// Deterministic request routing shared by browser providers.
(function (root) {
  'use strict';

  function classifyRequestType(message) {
    var text = String(message || '').toLowerCase();
    if (/\b(stock price|share price|stock market|stock quote|market cap|ticker symbol|nasdaq|s&p ?500|dow jones|earnings report)\b/.test(text) ||
        /(?:^|\s)\$[a-z]{1,5}\b/.test(text) && /\b(price|quote|market|trading|trade|buy|sell|invest|worth|value)\b/.test(text)) {
      return 'financial-data';
    }
    if (/\b(weather|forecast|temperature|raining|snowing|humidity|wind speed)\b/.test(text)) {
      return 'weather-search';
    }
    if (/\b(news|headlines?|breaking news|current events?|morning (?:briefing|report|update)|daily (?:briefing|report|update)|briefing)\b/.test(text) ||
        /\blatest\b.*\b(update|report|story|stories|happening|developments?)\b/.test(text)) {
      return 'news-search';
    }
    if (/\b(kql|run a query|execute a query|table schema|sample rows|show me data)\b/.test(text) ||
        /\bkusto\b/.test(text) && /\b(?:query|table|rows?|data|schema|count|filter|summarize)\b/.test(text)) {
      return 'kusto-query';
    }
    if (/\b(count|summarize|filter by|group by|join|distinct|top \d|take \d)\b/.test(text)) {
      return 'kusto-operator';
    }
    var githubOperation = /\b(?:search|find|list|check|review|show|get|open|create|update|close|comment|manage|run|query|merge|delete|push|compare|monitor|trigger)\b/.test(text);
    var githubSubject = /\b(?:github|github\.com)\b/.test(text);
    var githubExplanation = /^\s*(?:what|how|why|explain|describe)\b/.test(text) || /^\s*tell me about\b/.test(text);
    if (githubSubject && githubOperation && !githubExplanation) {
      return 'github-data';
    }
    if (/\b(search the web|web search|look up|google|what happened|who won|search for)\b/.test(text)) {
      return 'web-search';
    }
    return 'general';
  }

  function needsAcpPreflight(message, requestType) {
    var text = String(message || '').toLowerCase();
    var type = requestType || classifyRequestType(text);
    if (['news-search', 'weather-search', 'financial-data', 'web-search', 'github-data', 'kusto-query', 'kusto-operator'].indexOf(type) >= 0) {
      return true;
    }
    var explanatory = /^\s*(?:what|how|why|explain|describe|tell\s+me\s+about)\b/.test(text);
    var operation = '(?:search|find|list|check|review|open|create|update|close|comment|manage|run|trigger|configure|connect|deploy|query|enable|disable|start|stop|merge|delete|push|scale|restart|apply|get|describe)';
    var github = new RegExp('\\bgithub\\b[^.!?]{0,100}\\b' + operation + '\\b|\\b' + operation + '\\b[^.!?]{0,100}\\bgithub\\b').test(text);
    var platform = /\b(?:azure|mcp|kubernetes|kubectl|kusto)\b/.test(text) && new RegExp('\\b' + operation + '\\b').test(text);
    var artifact = /\b(?:create|generate|make|export|download)\b[^.!?]{0,100}\b(?:pdf|csv|json|markdown|md|txt|file|report|document|spreadsheet|invoice)\b|\bwrite\s+(?:a\s+|an\s+|the\s+)?(?:pdf|csv|json|markdown|md|txt|file|report|document|spreadsheet|invoice)\b|\bsave\s+(?:this|that|it)\s+as\s+(?:a\s+|an\s+)?(?:pdf|csv|json|markdown|md|txt|file|report|spreadsheet)\b/.test(text);
    return artifact || (!explanatory && (github || platform));
  }

  function isExplicitInteractiveRequest(message) {
    var text = String(message || '');
    var interaction = '\\b(?:use|open|launch|control|click|navigate|check)\\b';
    var interfaceName = '\\b(?:browser|desktop|website|web site|app)\\b';
    var weather = '\\b(?:weather|forecast|temperature)\\b';
    return new RegExp(
      interaction + '[^.!?]{0,40}' + interfaceName + '|' +
      interfaceName + '[^.!?]{0,40}' + interaction + '|' +
      weather + '[^.!?]{0,80}' + interfaceName + '|' +
      interfaceName + '[^.!?]{0,80}' + weather,
      'i'
    ).test(text);
  }

  function isNativeWeatherLookup(message) {
    return /\b(?:weather|forecast|temperature|raining|snowing|humidity|wind speed)\b/i.test(String(message || '')) &&
      !isExplicitInteractiveRequest(message);
  }

  function isNarrowNativeOperation(message) {
    var text = String(message || '');
    if (isExplicitInteractiveRequest(text)) return false;
    var type = classifyRequestType(text);
    if (['news-search', 'weather-search', 'financial-data', 'web-search', 'github-data', 'kusto-query', 'kusto-operator'].indexOf(type) >= 0) return true;
    return /\b(?:create|generate|make|export|download|write|save|edit|replace|validate|inspect|merge|split|extract|recalculate|scaffold)\b[^.!?]{0,100}\b(?:pdf|docx|pptx|xlsx|csv|json|markdown|md|txt|file|report|document|spreadsheet|invoice|presentation|mcp\s+server|fastmcp)\b/i.test(text);
  }

  function createTurnId() {
    if (root.crypto && typeof root.crypto.randomUUID === 'function') return 'turn-' + root.crypto.randomUUID();
    return 'turn-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 14);
  }

  root.EvaRequestRouting = {
    classifyRequestType: classifyRequestType,
    isGitHubOperation: function(message) { return classifyRequestType(message) === 'github-data'; },
    needsAcpPreflight: needsAcpPreflight,
    isExplicitInteractiveRequest: isExplicitInteractiveRequest,
    isNativeWeatherLookup: isNativeWeatherLookup,
    isNarrowNativeOperation: isNarrowNativeOperation,
    createTurnId: createTurnId,
    needsDataRetrieval: function (message) {
      return needsAcpPreflight(message, classifyRequestType(message));
    }
  };
}(typeof window !== 'undefined' ? window : this));