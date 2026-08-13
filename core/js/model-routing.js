// Deterministic model-selection routing shared by browser send controls.
(function (root) {
  'use strict';

  var directOpenAIModels = new Set([
    'gpt-4o', 'gpt-4o-mini', 'o1', 'o1-preview', 'o1-mini', 'o3-mini',
    'gpt-5-mini', 'latest'
  ]);

  function routeFor(model) {
    var value = String(model || '');
    if (value === 'aig') return 'aig';
    if (value.indexOf('copilot-') === 0) return 'copilot';
    if (directOpenAIModels.has(value)) return 'openai';
    if (value === 'gemini') return 'gemini';
    if (value === 'lm-studio') return 'lmstudio';
    if (value === 'dall-e-3') return 'image';
    return '';
  }

  root.EvaModelRouting = {
    routeFor: routeFor
  };
}(typeof window !== 'undefined' ? window : this));