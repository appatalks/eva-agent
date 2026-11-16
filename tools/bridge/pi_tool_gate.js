export default function (pi) {
  const mcpTools = JSON.parse(process.env.EVA_PI_MCP_TOOLS || '[]');
  for (const tool of mcpTools) {
    pi.registerTool({
      name: tool.name,
      label: tool.name,
      description: tool.description,
      parameters: tool.parameters,
      async execute(toolCallId, input, _signal, _onUpdate, ctx) {
        const request = JSON.stringify({ id: toolCallId, tool: tool.name, input });
        const response = await ctx.ui.input('Eva MCP tool call', request, { timeout: 90000 });
        let result;
        try { result = JSON.parse(response || '{}'); } catch (_) { result = {}; }
        return { content: [{ type: 'text', text: String(result.text ?? result.error ?? 'MCP tool unavailable') }],
           details: {}, isError: !!result.error || typeof result.text !== 'string' };
      }
    });
  }
  pi.on('session_start', async (_event, ctx) => {
    ctx.ui.notify('EVA_PI_GATE_READY_V1');
  });
  pi.on('tool_call', async (event, ctx) => {
    const message = JSON.stringify({ id: event.toolCallId, tool: event.toolName, input: event.input });
    if (!ctx.hasUI || message.length > 8192 || !(await ctx.ui.confirm('Eva Pi tool approval', message, { timeout: 90000 }))) {
      return { block: true, reason: 'Eva did not approve this tool call', terminate: true };
    }
  });
}