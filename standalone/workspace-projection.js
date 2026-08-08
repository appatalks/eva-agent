function redactKnownPaths(value, paths) {
  let text = String(value || '');
  const knownPaths = Array.from(new Set((paths || [])
    .filter(function(item) { return typeof item === 'string' && item.length > 1; })))
    .sort(function(left, right) { return right.length - left.length; });
  knownPaths.forEach(function(knownPath) {
    text = text.split(knownPath).join('<workspace>');
    const alternate = knownPath.indexOf('\\') === -1
      ? knownPath.replace(/\//g, '\\')
      : knownPath.replace(/\\/g, '/');
    if (alternate !== knownPath) text = text.split(alternate).join('<workspace>');
  });
  return text;
}

module.exports = { redactKnownPaths: redactKnownPaths };
