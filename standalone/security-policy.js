'use strict';

const net = require('net');

const EGRESS_MODES = Object.freeze(['offline', 'local-network', 'cloud']);

function normalizeEgressMode(raw) {
	const value = String(raw || '').trim().toLowerCase();
	if (!value) return 'cloud';
	if (EGRESS_MODES.indexOf(value) < 0) {
		throw new Error('EVA_EGRESS_MODE must be offline, local-network, or cloud.');
	}
	return value;
}

function cleanHost(hostname) {
	return String(hostname || '').toLowerCase().replace(/^\[|\]$/g, '');
}

function isLoopbackHost(hostname) {
	const host = cleanHost(hostname);
	return host === 'localhost' || host === '127.0.0.1' || host === '::1';
}

function isPrivateLiteral(hostname) {
	const host = cleanHost(hostname);
	if (isLoopbackHost(host)) return true;
	if (net.isIP(host) === 4) {
		const parts = host.split('.').map(Number);
		return parts[0] === 10 ||
			(parts[0] === 172 && parts[1] >= 16 && parts[1] <= 31) ||
			(parts[0] === 192 && parts[1] === 168) ||
			(parts[0] === 169 && parts[1] === 254);
	}
	if (net.isIP(host) === 6) {
		return host.startsWith('fc') || host.startsWith('fd') ||
			host.startsWith('fe8') || host.startsWith('fe9') ||
			host.startsWith('fea') || host.startsWith('feb');
	}
	return false;
}

function requestAllowedByEgress(rawUrl, mode) {
	const activeMode = normalizeEgressMode(mode);
	if (activeMode === 'cloud') return true;
	let parsed;
	try { parsed = new URL(rawUrl); } catch (_) { return false; }
	if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return false;
	if (activeMode === 'offline') return isLoopbackHost(parsed.hostname);
	return isPrivateLiteral(parsed.hostname);
}

module.exports = Object.freeze({
	EGRESS_MODES,
	normalizeEgressMode,
	isLoopbackHost,
	isPrivateLiteral,
	requestAllowedByEgress
});