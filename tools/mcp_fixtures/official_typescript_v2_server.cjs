/* Minimal official TypeScript MCP SDK v2 stdio server for Eva interoperability tests. */
const { McpServer } = require('@modelcontextprotocol/server');
const { serveStdio } = require('@modelcontextprotocol/server/stdio');
const { z } = require('zod');

function createServer() {
  const server = new McpServer(
    { name: 'eva-official-typescript-fixture', version: '2.0.0' },
    { capabilities: { tools: {} } }
  );

  server.registerTool(
    'official_typescript_echo',
    {
      description: 'Return deterministic text to prove Eva completed a modern MCP tool call.',
      inputSchema: { value: z.string() }
    },
    async ({ value }) => ({ content: [{ type: 'text', text: 'typescript:' + value }] })
  );
  return server;
}

try {
  serveStdio(createServer);
} catch (error) {
  process.stderr.write(String(error && error.stack || error) + '\n');
  process.exit(1);
}