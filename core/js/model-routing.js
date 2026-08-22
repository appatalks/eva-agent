// Deterministic model-selection routing shared by browser send controls.
(function (root) {
  'use strict';

  function routeFor(model) {
    var value = String(model || '');
    return value === 'aig' ? 'aig' : '';
  }

  root.EvaModelRouting = {
    routeFor: routeFor
  };
}(typeof window !== 'undefined' ? window : this));