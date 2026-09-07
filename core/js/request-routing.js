// Deterministic request routing shared by browser providers.
(function (root) {
  'use strict';

  var RESEARCH_MAX_QUERY_LENGTH = 500;
  var RESEARCH_STRATEGIES = { search: true, refine: true, alternate: true };
  var RESEARCH_FOLLOWUP_RE = /^(?:(?:please\s+)?(?:continue|continue (?:the )?research|keep going|keep researching|go on|carry on|more|find more|what else|dig deeper|go deeper|refine(?: it| this)?|narrow(?: it| this)?(?: down)?|try a different search method|use (?:a )?different search method|use another (?:search|source|method)|another search|different sources?)|go ahead and try a different search method)\s*[.!?]*$/i;
  var DIRECT_DEEP_RESEARCH_RE = /\b(?:deep\s+dive|deep\s+research|detailed\s+research|thorough\s+research)\s+(?:on|into|about|for)\s+(.+)/i;
  var RESEARCH_ALTERNATE_RE = /\b(?:different search method|another search|alternate method|different sources?)\b/i;
  var RESEARCH_REFINE_RE = /\b(?:refine|narrow|dig deeper|go deeper|find more|what else|keep going|go on)\b/i;
  var RESEARCH_PRIVATE_RE = /\b(?:email|e-mail|mail|inbox|secret|password|token|credential|credentials|body|private|api key|access key|auth(?:entication|orization)?)\b/i;
  var RESEARCH_OFFLINE_RE = /\b(?:offline|local|document|documents|file|files|folder|folders|notes?|paper|library|desktop)\b/i;
  var RESEARCH_STOP_WORDS = {
    a: true, an: true, about: true, and: true, are: true, do: true, for: true,
    in: true, into: true, is: true, it: true, on: true, online: true,
    research: true, the: true, this: true, that: true, topic: true, web: true,
    with: true
  };

  function stripResearchMarkers(text) {
    return String(text || '').replace(/\[\[[\s\S]{0,240}?\]\]/g, ' ');
  }

  function cleanResearchText(value) {
    var text = String(value || '').trim();
    text = text.replace(/^\s*<(?:user|human)>\s*|\s*<\/(?:user|human)>\s*$/ig, '');
    text = text.replace(/^\s*(?:\[\s*(?:current\s+)?(?:user|request|message)\s*\]|(?:current\s+)?user(?:\s+message)?|request)\s*:\s*/i, '');
    return stripResearchMarkers(text).trim();
  }

  function isQuotedResearchCommand(text) {
    var trigger = /\b(?:research|search\s+the\s+web|web\s+search|look\s+up|investigate|find)\b/i;
    var patterns = [/["'][^"'\n]{0,500}["']/g, /`[^`\n]{0,500}`/g, /“[^”\n]{0,500}”/g];
    return patterns.some(function (pattern) {
      var match;
      while ((match = pattern.exec(text)) !== null) {
        if (trigger.test(match[0].slice(1, -1))) return true;
      }
      return false;
    });
  }

  function isExplicitBrowserResearchAction(text) {
    var lowered = String(text || '').toLowerCase();
    if (!/\b(?:browser|desktop|chrome|firefox|safari|edge)\b/.test(lowered)) return false;
    return /\b(?:open|launch|use|click|navigate|visit|go\s+to|through|in|on)\b[^.!?\n]{0,80}\b(?:browser|desktop|chrome|firefox|safari|edge)\b|\b(?:browser|desktop|chrome|firefox|safari|edge)\b[^.!?\n]{0,80}\b(?:click|open|navigate|visit)\b/.test(lowered);
  }

  function researchDomainConflict(text) {
    var lowered = String(text || '').toLowerCase();
    if (/\b(?:research|investigate|deep\s+dive)\b/.test(lowered)) return false;
    if (/\b(?:stock|share|ticker|quote|price)\b/.test(lowered) && /\b(?:current|latest|today|buy|sell|show|get|check|price|quote)\b/.test(lowered)) return true;
    if (/\b(?:weather|forecast|temperature)\b/.test(lowered) && /\b(?:current|today|tomorrow|latest|show|get|check|what)\b/.test(lowered)) return true;
    if (/\b(?:email|e-mail|inbox|mail|messages?)\b/.test(lowered) && /\b(?:read|check|search|find|list|send|reply|latest|unread|open)\b/.test(lowered)) return true;
    if (/\b(?:github|pull\s+request|repository|repo|issue)\b/.test(lowered) && /\b(?:open|check|list|find|review|comment|create|close|merge|status)\b/.test(lowered)) return true;
    return false;
  }

  function isResearchRequest(text) {
    var cleaned = cleanResearchText(text);
    if (!cleaned || isQuotedResearchCommand(cleaned)) return false;
    var lowered = cleaned.toLowerCase();
    if (/\b(?:do\s+not|don't|dont|never|no|without)\s+(?:a\s+)?(?:web\s+)?(?:search|research|look\s+up|investigate|find)\b|\bnot\s+(?:search|research)\b/.test(lowered)) return false;
    if (isExplicitBrowserResearchAction(cleaned) || researchDomainConflict(cleaned)) return false;
    if (/\b(?:what\s+is|define|definition\s+of|meaning\s+of)\s+research\b/.test(lowered)) return false;
    if (RESEARCH_OFFLINE_RE.test(cleaned) && !/\b(?:online|web|internet)\b/.test(lowered)) return false;
    if (/^research(?:\s+methods?)?\s*[.!?]*$/i.test(lowered)) return false;
    if (DIRECT_DEEP_RESEARCH_RE.test(cleaned)) return true;
    if (RESEARCH_FOLLOWUP_RE.test(cleaned)) return true;
    return /\b(?:deep\s+dive\s+online\s+research|online\s+research\s+(?:on|about|into|for)|search\s+the\s+web|web\s+search|look\s+up\s+|investigate\s+.+\s+online|find\s+.+\s+online|research\s+.+|compare\s+.+\s+online)\b/i.test(lowered);
  }

  function isResearchFollowup(text) {
    var cleaned = cleanResearchText(text);
    if (!cleaned || /\b(?:stop|cancel|never\s+mind|forget\s+it)\b/i.test(cleaned)) return false;
    return RESEARCH_FOLLOWUP_RE.test(cleaned);
  }

  function researchTopicCleanup(value) {
    var topic = stripResearchMarkers(String(value || '')).trim().replace(/^[ \t\r\n.,;:!?-]+|[ \t\r\n.,;:!?-]+$/g, '');
    topic = topic.replace(/^(?:please\s+|can\s+you\s+|could\s+you\s+|would\s+you\s+)/i, '');
    topic = topic.replace(/^(?:on|about|into|for|regarding)\s+/i, '');
    topic = topic.replace(/\s+(?:online|on\s+the\s+web|using\s+(?:the\s+)?web)\s*$/i, '');
    topic = topic.replace(/\s+please\s*$/i, '');
    return topic.replace(/\s+/g, ' ').trim().replace(/^[ \t\r\n.,;:!?-]+|[ \t\r\n.,;:!?-]+$/g, '').slice(0, RESEARCH_MAX_QUERY_LENGTH);
  }

  function weakResearchTopic(topic) {
    var words = String(topic || '').toLowerCase().match(/[a-z0-9][a-z0-9._+-]*/g) || [];
    return !words.length || (words.length <= 2 && words.every(function (word) { return RESEARCH_STOP_WORDS[word]; }));
  }

  function privateResearchTopic(topic) {
    return !topic || RESEARCH_PRIVATE_RE.test(topic);
  }

  function directResearchInfo(text) {
    var cleaned = cleanResearchText(text);
    if (!isResearchRequest(cleaned) || isResearchFollowup(cleaned)) return { explicit: false, strategy: 'search', topic: '' };
    var deepDive = DIRECT_DEEP_RESEARCH_RE.exec(cleaned);
    if (deepDive) return { explicit: true, strategy: 'refine', topic: researchTopicCleanup(deepDive[1]) };
    var strategy = RESEARCH_ALTERNATE_RE.test(cleaned) ? 'alternate' : 'search';
    if (strategy !== 'alternate' && RESEARCH_REFINE_RE.test(cleaned)) strategy = 'refine';
    var patterns = [
      /deep\s+dive\s+online\s+research(?:\s+(?:on|about|into|for))?\s*(.*)$/i,
      /online\s+research\s+(?:on|about|into|for)\s+(.+)$/i,
      /(?:search\s+the\s+web|web\s+search)\s*(?:for|about|on|regarding)?\s*(.*)$/i,
      /look\s+up\s+(.+)$/i,
      /investigate\s+(.+?)\s+online\s*$/i,
      /find\s+(.+?)\s+online\s*$/i,
      /compare\s+(.+?)\s+online\s*$/i,
      /research\s+(?:on|about|into|for)\s+(.+)$/i,
      /research\s+(.+)$/i
    ];
    var topic = '';
    for (var index = 0; index < patterns.length; index++) {
      var match = patterns[index].exec(cleaned);
      if (match) {
        topic = researchTopicCleanup(match[1]);
        break;
      }
    }
    if (RESEARCH_FOLLOWUP_RE.test(cleaned)) topic = '';
    return { explicit: true, strategy: strategy, topic: topic };
  }

  function researchMessageText(message) {
    if (!message || String(message.role || '').toLowerCase() !== 'user') return '';
    var content = message.content;
    if (typeof content === 'string') return cleanResearchText(content);
    if (Array.isArray(content)) {
      return cleanResearchText(content.filter(function (item) {
        return item && typeof item.text === 'string';
      }).map(function (item) { return item.text; }).join(' '));
    }
    return '';
  }

  function researchPriorUsers(messages, current) {
    if (!Array.isArray(messages)) return [];
    var users = messages.slice(-12).map(researchMessageText).filter(Boolean);
    var currentKey = String(current || '').toLowerCase();
    while (users.length && users[users.length - 1].toLowerCase() === currentKey) users.pop();
    return users.slice(-6);
  }

  function researchQuestionTopic(text) {
    var cleaned = cleanResearchText(text);
    var lowered = cleaned.toLowerCase();
    if (!cleaned || isResearchFollowup(cleaned) || isResearchRequest(cleaned)) return '';
    if (/\b(?:what\s+time|how\s+are\s+you|tell\s+me\s+a\s+joke|thanks?|thank\s+you|stop|cancel)\b/.test(lowered)) return '';
    var topic = researchTopicCleanup(cleaned);
    if ((topic.match(/\w+/g) || []).length < 3 || privateResearchTopic(topic)) return '';
    return topic;
  }

  function researchContextQuery(priorUsers, allowQuestion) {
    if (!priorUsers.length) return '';
    var index = priorUsers.length - 1;
    var latest = directResearchInfo(priorUsers[index]);
    if (latest.explicit) {
      var directTopic = latest.topic;
      if (directTopic && !privateResearchTopic(directTopic) && !weakResearchTopic(directTopic)) return directTopic;
      if (index) {
        var priorQuestion = researchQuestionTopic(priorUsers[index - 1]);
        if (priorQuestion) return priorQuestion;
      }
      return '';
    }
    if (isResearchFollowup(priorUsers[index])) {
      var cursor = index;
      while (cursor >= 0 && isResearchFollowup(priorUsers[cursor])) cursor--;
      if (cursor < 0) return '';
      var origin = directResearchInfo(priorUsers[cursor]);
      if (!origin.explicit) return '';
      var originTopic = origin.topic;
      if ((!originTopic || weakResearchTopic(originTopic)) && cursor) originTopic = researchQuestionTopic(priorUsers[cursor - 1]);
      return originTopic && !privateResearchTopic(originTopic) ? originTopic : '';
    }
    return allowQuestion ? researchQuestionTopic(priorUsers[index]) : '';
  }

  function resolveResearchRequest(userMessage, messages) {
    var request = cleanResearchText(userMessage);
    var plan = {
      active: false,
      query: '',
      strategy: 'search',
      needs_topic: false,
      continuation: false,
      request: request
    };
    var priorUsers = researchPriorUsers(messages, request);
    var direct = directResearchInfo(request);
    if (direct.explicit) {
      var topic = direct.topic;
      if (!topic || weakResearchTopic(topic)) topic = researchContextQuery(priorUsers, true);
      plan.active = true;
      plan.query = privateResearchTopic(topic) ? '' : String(topic || '').slice(0, RESEARCH_MAX_QUERY_LENGTH);
      plan.strategy = RESEARCH_STRATEGIES[direct.strategy] ? direct.strategy : 'search';
      plan.needs_topic = !plan.query;
      return plan;
    }
    if (isResearchFollowup(request)) {
      var contextTopic = researchContextQuery(priorUsers, false);
      if (!contextTopic) {
        if (RESEARCH_ALTERNATE_RE.test(request)) {
          plan.active = true;
          plan.needs_topic = true;
          plan.strategy = 'alternate';
        }
        return plan;
      }
      plan.active = true;
      plan.query = contextTopic.slice(0, RESEARCH_MAX_QUERY_LENGTH);
      plan.strategy = RESEARCH_ALTERNATE_RE.test(request) ? 'alternate' : 'refine';
      plan.continuation = true;
    }
    return plan;
  }

  function getResearchHistory(messages) {
    if (!Array.isArray(messages)) return [];
    return messages.map(researchMessageText).filter(Boolean).slice(-6);
  }

  function classifyRequestType(message) {
    var text = String(message || '').toLowerCase();
    if (/\b(?:send|sending|compose|composing|draft|drafting|write|writing)\b[^.!?]{0,80}\b(?:email|e-mail|mail)\b|\b(?:email|e-mail|mail)\b[^.!?]{0,80}\b(?:send|sending|compose|composing|draft|drafting|write|writing)\b/.test(text)) {
      return 'email-action';
    }
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
    var githubOperation = /\b(?:search|find|list|check|review|show|get|open|create|submit|publish|post|update|close|comment|manage|run|query|merge|delete|push|compare|monitor|trigger)\b/.test(text);
    var githubSubject = /\b(?:github|github\.com)\b/.test(text);
    var githubExplanation = /^\s*(?:what|how|why|explain|describe)\b/.test(text) || /^\s*tell me about\b/.test(text);
    if (githubSubject && githubOperation && !githubExplanation) {
      return 'github-data';
    }
    if (resolveResearchRequest(message, []).active) {
      return 'web-search';
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
    var operation = '(?:search|find|list|check|review|open|create|submit|publish|post|update|close|comment|manage|run|trigger|configure|connect|deploy|query|enable|disable|start|stop|merge|delete|push|scale|restart|apply|get|describe)';
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

  function isExplicitCameraRequest(message) {
    var text = String(message || '');
    if (/\b(?:do not|don'?t|never|without|avoid|disable|turn off|stop)\b[^.!?]{0,50}\b(?:camera|webcam|look|see)\b/i.test(text)) return false;
    if (!/\b(?:camera|webcam)\b/i.test(text)) return false;
    return /\b(?:use|open|enable|start|turn on|activate|access|check)\b[^.!?]{0,50}\b(?:camera|webcam)\b|\b(?:camera|webcam)\b[^.!?]{0,50}\b(?:use|open|enable|start|turn on|activate|access|check|look|see)\b|\b(?:look|see)\s+through\s+(?:the\s+)?(?:camera|webcam)\b/i.test(text);
  }

  function isNativeWeatherLookup(message) {
    return /\b(?:weather|forecast|temperature|raining|snowing|humidity|wind speed)\b/i.test(String(message || '')) &&
      !isExplicitInteractiveRequest(message);
  }

  function isNarrowNativeOperation(message) {
    var text = String(message || '');
    if (isExplicitInteractiveRequest(text)) return false;
    var type = classifyRequestType(text);
    if (['news-search', 'weather-search', 'financial-data', 'web-search', 'github-data', 'kusto-query', 'kusto-operator', 'email-action'].indexOf(type) >= 0) return true;
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
    isExplicitCameraRequest: isExplicitCameraRequest,
    isNativeWeatherLookup: isNativeWeatherLookup,
    isNarrowNativeOperation: isNarrowNativeOperation,
    createTurnId: createTurnId,
    isResearchRequest: isResearchRequest,
    isResearchFollowup: isResearchFollowup,
    resolveResearchRequest: resolveResearchRequest,
    getResearchHistory: getResearchHistory,
    needsDataRetrieval: function (message, messages) {
      if (resolveResearchRequest(message, messages).active) return true;
      return needsAcpPreflight(message, classifyRequestType(message));
    }
  };
}(typeof window !== 'undefined' ? window : this));