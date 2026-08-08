function isExternalUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === 'http:' || url.protocol === 'https:';
  } catch (_) {
    return false;
  }
}

function buildContextMenuTemplate(params, actions) {
  const context = params || {};
  const callbacks = actions || {};
  const editFlags = context.editFlags || {};
  const template = [];
  const appendEditItem = function(label, role, enabled) {
    template.push({ label: label, role: role, enabled: !!enabled });
  };

  if (context.isEditable) {
    appendEditItem('Undo', 'undo', editFlags.canUndo);
    appendEditItem('Redo', 'redo', editFlags.canRedo);
    template.push({ type: 'separator' });
    appendEditItem('Cut', 'cut', editFlags.canCut);
    appendEditItem('Copy', 'copy', editFlags.canCopy);
    appendEditItem('Paste', 'paste', editFlags.canPaste);
    appendEditItem('Paste as Plain Text', 'pasteAndMatchStyle', editFlags.canPaste);
    appendEditItem('Delete', 'delete', editFlags.canDelete);
    template.push({ type: 'separator' });
    appendEditItem('Select All', 'selectAll', editFlags.canSelectAll);
  } else if (context.selectionText) {
    appendEditItem('Copy', 'copy', editFlags.canCopy);
    template.push({ type: 'separator' });
    appendEditItem('Select All', 'selectAll', editFlags.canSelectAll);
  }

  if (isExternalUrl(context.linkURL)) {
    if (template.length) template.push({ type: 'separator' });
    template.push({ label: 'Open Link in Browser', click: function() { callbacks.openExternal(context.linkURL); } });
    template.push({ label: 'Copy Link Address', click: function() { callbacks.copyText(context.linkURL); } });
  }
  if (context.mediaType === 'image') {
    if (template.length) template.push({ type: 'separator' });
    template.push({ label: 'Copy Image', click: function() { callbacks.copyImage(context.x, context.y); } });
    if (isExternalUrl(context.srcURL)) {
      template.push({ label: 'Open Image in Browser', click: function() { callbacks.openExternal(context.srcURL); } });
    }
  }

  return template;
}

module.exports = { buildContextMenuTemplate, isExternalUrl };