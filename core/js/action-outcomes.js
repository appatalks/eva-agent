// Exact public validation for eva.action-run/1 outcomes.
(function(root, factory) {
  var api = factory();
  root.EvaActionOutcomes = api;
  if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function() {
  'use strict';

  var HEX64 = /^[0-9a-f]{64}$/;
  var CHECK_IDS = {
    'browser.url_match': 'browser-url',
    'browser.element_state': 'browser-element',
    'desktop.process_spawned': 'desktop-process'
  };
  var AFFIRMATIVE = new Set([
    'yes', 'yes please', 'yep', 'yeah', 'yup', 'ok', 'okay', 'sure',
    'approve', 'approve it', 'i approve', 'i approve this action',
    'confirm', 'confirmed', 'go ahead', 'proceed', 'please proceed',
    'do it', 'please do', 'affirmative'
  ]);
  var NEGATIVE = /\b(no|nope|nah|not|never|cannot|can't|wont|won't|dont|don't|deny|decline|cancel|stop|abort)\b/;
  var UNCERTAIN = /\b(maybe|perhaps|unsure|uncertain|wait|hold|later|think|guess)\b/;

  function exactKeys(value, expected) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
    var keys = Object.keys(value).sort();
    var wanted = expected.slice().sort();
    return keys.length === wanted.length && keys.every(function(key, index) {
      return key === wanted[index];
    });
  }

  function validEvidence(evidence, type) {
    return exactKeys(evidence, ['kind', 'source', 'captured_at', 'step', 'digest']) &&
      evidence.kind === type && evidence.source === 'tool' &&
      typeof evidence.captured_at === 'string' &&
      Number.isInteger(evidence.step) && evidence.step >= 0 && evidence.step <= 60 &&
      typeof evidence.digest === 'string' && HEX64.test(evidence.digest);
  }

  function validCheck(check) {
    if (!exactKeys(check, ['check_id', 'type', 'verdict', 'evidence']) ||
        check.verdict !== 'observed' || CHECK_IDS[check.type] !== check.check_id ||
        !Array.isArray(check.evidence) || check.evidence.length !== 1) return false;
    return validEvidence(check.evidence[0], check.type);
  }

  function isVerifiedSuccess(status) {
    if (!status || status.contract_version !== 'eva.action-run/1') return false;
    var outcome = status.outcome;
    if (!exactKeys(outcome, [
      'state', 'reason', 'termination', 'model_claim', 'postcondition', 'proof',
      'started_at', 'finished_at', 'duration_ms'
    ]) || outcome.state !== 'succeeded' ||
        typeof outcome.reason !== 'string' || typeof outcome.started_at !== 'string' ||
        typeof outcome.finished_at !== 'string' || !Number.isInteger(outcome.duration_ms) ||
        outcome.duration_ms < 0 || outcome.duration_ms > 86400000) return false;
    if (!exactKeys(outcome.termination, ['cause', 'step']) ||
        typeof outcome.termination.cause !== 'string' ||
        !Number.isInteger(outcome.termination.step) || outcome.termination.step < 0 ||
        outcome.termination.step > 60) return false;
    if (!exactKeys(outcome.model_claim, ['summary_hash']) ||
        !HEX64.test(outcome.model_claim.summary_hash || '')) return false;
    var post = outcome.postcondition;
    if (!exactKeys(post, ['verdict', 'spec_source', 'verified_by', 'spec_hash', 'checks']) ||
        post.verdict !== 'observed' || post.spec_source !== 'request' ||
        post.verified_by !== 'tool' || !HEX64.test(post.spec_hash || '') ||
        !Array.isArray(post.checks) || post.checks.length !== 1 ||
        !validCheck(post.checks[0])) return false;
    var proof = outcome.proof;
    if (!exactKeys(proof, [
      'baseline_verdict', 'effect_count', 'effect_receipt_digests'
    ]) || proof.baseline_verdict !== 'not_observed' ||
        !Number.isInteger(proof.effect_count) || proof.effect_count < 1 ||
        !Array.isArray(proof.effect_receipt_digests) ||
        proof.effect_receipt_digests.length !== proof.effect_count ||
        !proof.effect_receipt_digests.every(function(digest) {
          return typeof digest === 'string' && HEX64.test(digest);
        })) return false;
    return true;
  }

  function displayState(status) {
    if (!status || !status.outcome || typeof status.outcome.state !== 'string') {
      return status && status.status === 'error' ? 'failed'
        : status && status.status === 'cancelled' ? 'aborted'
        : status && status.status === 'done' ? 'indeterminate' : '';
    }
    if (status.outcome.state === 'succeeded' && !isVerifiedSuccess(status)) {
      return 'indeterminate';
    }
    return status.outcome.state;
  }

  function classifyApprovalReply(value) {
    var text = String(value || '').normalize('NFC').trim().toLowerCase()
      .replace(/[.!?]+$/g, '').replace(/\s+/g, ' ');
    if (!text) return 'ambiguous';
    if (NEGATIVE.test(text) || /\bdo\s+not\b/.test(text)) return 'deny';
    if (UNCERTAIN.test(text)) return 'ambiguous';
    return AFFIRMATIVE.has(text) ? 'approve' : 'ambiguous';
  }

  return Object.freeze({
    isVerifiedSuccess: isVerifiedSuccess,
    displayState: displayState,
    classifyApprovalReply: classifyApprovalReply
  });
});
