// Auto-learned Skills from successful agent outcomes.
async function autoLearnSkill(messages, taskSummary) {
  try {
    var bridgeUrl = await detectACPBridge();
    var response = await fetch(bridgeUrl.replace(/\/+$/, '') + '/v1/skills/auto-learn', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ messages: messages || [], task_summary: taskSummary || '' })
    });
    var data = await response.json();
    if (response.ok && data.skill) {
      setStatus('info', 'Skill draft learned: ' + (data.skill.Name || 'untitled'));
      return data.skill;
    }
    return null;
  } catch (_) {
    return null;
  }
}