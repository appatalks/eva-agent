#!/usr/bin/env node
'use strict';

const assert = require('assert');
const fs = require('fs');

const source = fs.readFileSync('core/js/providers/aig.js', 'utf8');
const options = fs.readFileSync('core/js/options.js', 'utf8');
const bridge = fs.readFileSync('tools/bridge/core.py', 'utf8');
const html = fs.readFileSync('index.html', 'utf8');
assert.match(source, /var briefingRequest = \/\\b\(\?:morning\|daily\)\\s\+\(\?:briefing\|report\|update\)\\b\/i/);
assert.match(source, /function formatPreparedBriefing\(status, preparing, requestedQuote\)/);
assert.match(source, /function requestedStockSymbol\(text\)/);
assert.match(source, /function readAigQuestionInput\(element\)/);
assert.match(source, /async function fetchBriefingQuote\(bridgeUrl, userMessage, sessionId\)/);
assert.match(source, /### Requested quote/);
assert.match(source, /async function waitForPreparedBriefing\(bridgeUrl, initialStatus, onProgress\)/);
assert.match(source, /var deadline = Date\.now\(\) \+ 30000;/);
assert.match(source, /briefingStatus = await waitForPreparedBriefing\(bridgeUrl, briefingStatus, function \(latest\)/);
assert.match(source, /if \(briefingRequest\) \{\s+cogDecision = \{ active: false, reason: 'briefing-cache' \}/);
assert.match(source, /\/v1\/briefing\/refresh/);
assert.match(source, /var requestedQuote = await fetchBriefingQuote\(bridgeUrl, sQuestion, sessionId\)/);
assert.match(source, /if \(isDirectQuoteRequest\) \{\s+var quotePreview = createEvaStreamingBubble\(txtOutput\);/);
assert.match(source, /var directQuote = await fetchBriefingQuote\(bridgeUrl, sQuestion, sessionId\)/);
assert.match(source, /Eva verified the current ' \+ requestedQuoteSymbol \+ ' quote\./);
assert.doesNotMatch(source, /getBriefingWeatherLocation/);
assert.match(source, /\/v1\/memory\/remember-facts/);
assert.match(source, /Durable-memory commit succeeded for this turn:/);
assert.match(source, /then answer any other request in the user message/);
assert.match(source, /if \(savedFacts && savedFacts\.status === 'saved'\)/);
assert.match(source, /Durable-memory preflight was unavailable for this turn/);
assert.match(source, /await renderEvaResponse\(briefingContent, txtOutput/);
assert.match(source, /Eva morning briefing - prepared live sources/);
assert.match(source, /Live briefing data is unavailable right now\. Please try again shortly\./);
assert.match(source, /### Market news/);
assert.match(source, /eva-briefing-preview/);
const briefingBranch = source.slice(source.indexOf('if (briefingRequest) {'), source.indexOf('if (cogDecision.active) {'));
assert.doesNotMatch(briefingBranch, /EVA_BROWSER|EVA_DESKTOP|\/v1\/browser\/run/);
assert.match(options, /protectedNeedsDataRetrieval = window\.EvaRequestRouting/);
assert.match(options, /nativeRoute && nativeRoute\.action === 'describe_memory_titles'/);
assert.doesNotMatch(options, /briefing_weather_location|getBriefingWeatherLocation/);
assert.doesNotMatch(html, /id="briefingWeatherLocation"/);
assert.match(bridge, /briefing_unavailable_sources\(_briefing_status\)/);
assert.match(bridge, /parsed_path == "\/v1\/briefing\/refresh"/);
assert.match(bridge, /parsed_path == "\/v1\/memory\/remember-location"/);
assert.match(bridge, /\[Morning Briefing Availability\]/);
assert.match(bridge, /Never call this a complete briefing/);
assert.match(bridge, /_policy_decision = select_model_policy/);
assert.match(bridge, /not _briefing_request and _st\.cognition_enabled/);

async function checkRequestedQuoteSection() {
	const sandbox = {
		AbortSignal: { timeout() { return {}; } },
		getBridgeCapabilityHeaders() { return {}; },
		fetch: async () => ({
			json: async () => ({ data: JSON.stringify({
				stock_quote: {
					symbol: 'PLG', exchange: 'NYSEAMERICAN', currency: 'USD',
					price: 1.64, change: 0.12, change_percent: 7.89
				}
			}) })
		})
	};
	require('vm').createContext(sandbox);
	require('vm').runInContext(source, sandbox);
	const request = 'Hi Eva, can you give me a morning briefing for today, and include the last price and analysis of the stock PLG';
	const verified = await sandbox.fetchBriefingQuote('http://localhost:8888', request, 'session');
	assert.strictEqual(verified.available, true);
	assert.match(verified.content, /PLG/);
	assert.match(verified.content, /Session move/);
	const briefing = sandbox.formatPreparedBriefing({ sources: {} }, false, verified.content);
	assert.match(briefing, /Requested quote/);
	assert.match(briefing, /PLG/);
	const marketBriefing = sandbox.formatPreparedBriefing({ sources: {
		markets: { status: 'ready', summary: '- Sep 4 market close (Example - Fri, 04 Sep 2026 22:57:36 GMT)' }
	} }, false, '');
	assert.match(marketBriefing, /Market news/);
	assert.match(marketBriefing, /most recently completed U\.S\. trading session/);
	const partialBriefing = sandbox.formatPreparedBriefing({ sources: {
		weather: { status: 'failed', summary: 'Current weather conditions were not returned.' },
		news: { status: 'failed', summary: 'No timely current headlines were returned.' },
		markets: { status: 'ready', summary: '- Sep 4 market close (Example - Fri, 04 Sep 2026 22:57:36 GMT)' }
	} }, false, '');
	assert.doesNotMatch(partialBriefing, /### Weather|### Headlines/);
	assert.match(partialBriefing, /Live sources unavailable: weather, headlines\./);
	sandbox.fetch = async () => ({ json: async () => ({ data: '' }) });
	const unavailable = await sandbox.fetchBriefingQuote('http://localhost:8888', request, 'session');
	assert.strictEqual(unavailable.available, false);
	assert.match(unavailable.content, /PLG/);
	assert.match(unavailable.content, /unavailable/);
	const directRequest = 'What is the latest stock price of PYPL?';
	assert.strictEqual(sandbox.requestedStockSymbol(directRequest), 'PYPL');
	assert.match(sandbox.committedFactSummary([
		{ relation: 'correct_spelling', value: 'NewHandle' },
		{ relation: 'user_children', value: 'Nova, Rowan' },
		{ relation: 'user_motto', value: 'Evidence first' }
	]), /corrected spelling: NewHandle; family: Nova, Rowan; motto: Evidence first/);
	assert.strictEqual(sandbox.readAigQuestionInput({
		innerText: 'What did you find?',
		innerHTML: '<font face="Ubuntu"><span style="font-size: 12px">What did you find?</span></font>'
	}), 'What did you find?');
}

checkRequestedQuoteSection().then(() => {
	console.log('briefing route tests: PASS');
}).catch((error) => {
	console.error(error);
	process.exit(1);
});