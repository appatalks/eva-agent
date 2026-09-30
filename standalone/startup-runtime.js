const { execFile } = require('node:child_process');
const { promisify } = require('node:util');

const runCommand = promisify(execFile);
const pythonProbe = [
  'import importlib, json, sys',
  'modules = {}',
  'for name in ("requests", "cryptography.hazmat.primitives.ciphers.aead"):',
  '    try:',
  '        importlib.import_module(name)',
  '        modules[name] = True',
  '    except (ImportError, OSError):',
  '        modules[name] = False',
  'print(json.dumps({"version": list(sys.version_info[:3]), "modules": modules}))'
].join('\n');

async function probe(command, args, env, run) {
  try {
    return { stdout: (await run(command, args, { env, timeout: 12000, maxBuffer: 16384, windowsHide: true })).stdout };
  } catch (error) {
    if (error.code === 'ENOENT') return { error: 'not found' };
    if (error.code === 'EACCES') return { error: 'not executable' };
    if (error.killed) return { error: 'timed out' };
    if (typeof error.code === 'number') return { error: 'could not run successfully' };
    throw error;
  }
}

async function checkRuntime({ python, copilot, env = process.env, run = runCommand }) {
  const report = { blockers: [], notices: [] };
  const result = await probe(python.command, python.args.concat(['-c', pythonProbe]), env, run);
  if (result.error) {
    report.blockers.push('Python 3.12 or newer is required; the selected interpreter was ' + result.error + '.');
    return report;
  }

  let runtime;
  try {
    runtime = JSON.parse(result.stdout);
  } catch (error) {
    if (!(error instanceof SyntaxError)) throw error;
    throw new Error('Python returned an invalid runtime-check response.', { cause: error });
  }
  if (!Array.isArray(runtime.version) || runtime.version.length !== 3 ||
      !runtime.version.every(Number.isSafeInteger) || !runtime.modules ||
      typeof runtime.modules.requests !== 'boolean' ||
      typeof runtime.modules['cryptography.hazmat.primitives.ciphers.aead'] !== 'boolean') {
    throw new Error('Python returned an incomplete runtime-check response.');
  }
  if (runtime.version[0] < 3 || (runtime.version[0] === 3 && runtime.version[1] < 12)) {
    report.blockers.push('Python ' + runtime.version.join('.') + ' is too old; Eva requires Python 3.12 or newer.');
  }
  if (!runtime.modules.requests) report.blockers.push('The selected Python is missing a working requests package.');
  if (!runtime.modules['cryptography.hazmat.primitives.ciphers.aead']) {
    report.blockers.push('The selected Python is missing a working cryptography package.');
  }
  if (report.blockers.length) return report;

  const [node, cli] = await Promise.all([
    probe('node', ['--version'], env, run),
    probe(copilot || 'copilot', ['--version'], env, run)
  ]);
  const nodeVersion = node.error ? null : node.stdout.trim().match(/^v(\d+)\.\d+\.\d+(?:[-+].*)?$/);
  if (!nodeVersion || Number(nodeVersion[1]) < 24) {
    report.notices.push('Copilot ACP needs Node.js 24 or newer on your PATH.');
  }
  if (cli.error || !cli.stdout.trim()) {
    report.notices.push('Copilot CLI is unavailable. Install it and complete copilot auth login to use Copilot ACP.');
  }
  return report;
}

function setupInstructions(report, platform) {
  const instructions = [
    ...report.blockers,
    ...report.notices,
    '',
    'No dependencies have been installed and no settings or personal data have been changed.'
  ];
  if (platform === 'linux') {
    instructions.push(
      '',
      'Use the Eva setup helper, even if you downloaded only the AppImage:',
      'setup_file="$(mktemp)"',
      'curl -fsSL https://appatalks.github.io/eva-agent/get-eva.sh -o "$setup_file"',
      'Review the downloaded script, then run: bash "$setup_file"',
      'The helper sets up an installed copy; it does not configure your provider credentials.',
      'Alternatively, point EVA_PYTHON at a Python 3.12+ environment with requests and cryptography.'
    );
  } else if (platform === 'win32') {
    instructions.push('', 'Run the Eva Windows installer to provision its managed runtime, then restart Eva.');
  } else {
    instructions.push('', 'Use the source setup instructions to install Python 3.12+ with requests and cryptography, then restart Eva.');
  }
  instructions.push(
    '',
    'Copilot is optional for direct OpenAI or LM Studio chat.',
    'For Copilot ACP, complete copilot auth login before using that backend.',
    'After startup, select a backend in Settings > Models and configure credentials in Settings > Auth.',
    'Settings > General > Diagnostics checks optional browser, desktop, camera, and other capabilities.'
  );
  return instructions.join('\n');
}

module.exports = { checkRuntime, setupInstructions, pythonProbe };
