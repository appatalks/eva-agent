param(
  [switch]$LaunchLogin
)

$ErrorActionPreference = 'Stop'
$runtimeRoot = Join-Path $env:LOCALAPPDATA 'Eva Standalone\runtime'
$logPath = Join-Path $runtimeRoot 'bootstrap.log'
$manifestPath = Join-Path $runtimeRoot 'runtime.json'
New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null

function Write-BootstrapLog {
  param([string]$Message)
  $line = "$(Get-Date -Format o) $Message"
  Add-Content -Path $logPath -Value $line
  Write-Output $line
}

function Get-Python312 {
  try {
    & py -3.12 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" 2>$null
    if ($LASTEXITCODE -eq 0) { return 'py' }
  } catch {}
  return $null
}

function Get-Python311 {
  try {
    & py -3.11 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) and sys.version_info < (3, 12) else 1)" 2>$null
    if ($LASTEXITCODE -eq 0) { return 'py' }
  } catch {}
  return $null
}

function Get-Node24 {
  $candidates = @()
  $command = Get-Command node.exe -ErrorAction SilentlyContinue
  if ($command) { $candidates += $command.Source }
  if ($env:ProgramFiles) { $candidates += (Join-Path $env:ProgramFiles 'nodejs\node.exe') }
  if ($env:ProgramW6432) { $candidates += (Join-Path $env:ProgramW6432 'nodejs\node.exe') }
  if ($env:SystemDrive) { $candidates += (Join-Path $env:SystemDrive 'Program Files\nodejs\node.exe') }
  if (${env:ProgramFiles(x86)}) { $candidates += (Join-Path ${env:ProgramFiles(x86)} 'nodejs\node.exe') }
  if ($env:LOCALAPPDATA) { $candidates += (Join-Path $env:LOCALAPPDATA 'Programs\nodejs\node.exe') }

  foreach ($candidate in ($candidates | Select-Object -Unique)) {
    if (-not (Test-Path $candidate)) { continue }
    try {
      $version = (& $candidate --version 2>$null).Trim().TrimStart('v')
      if ([version]$version -ge [version]'24.0.0') { return $candidate }
    } catch {}
  }
  return $null
}

function Install-WingetPackage {
  param([string]$Id)
  Write-BootstrapLog "Installing $Id with winget."
  & winget install --id $Id --exact --source winget --silent --accept-package-agreements --accept-source-agreements
  if ($LASTEXITCODE -ne 0) { throw "winget failed to install $Id (exit code $LASTEXITCODE)." }
}

function Enable-LocalTranscription {
  param([string]$PythonLauncher, [string]$RuntimePath)
  $speechHome = Join-Path $RuntimePath 'speech'
  $speechPython = Join-Path $speechHome 'Scripts\python.exe'
  $speechError = ''
  try {
    & $PythonLauncher -3.12 -m venv $speechHome
    if ($LASTEXITCODE -ne 0) { throw "could not create the Local Voices environment (exit code $LASTEXITCODE)." }
    & $speechPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "could not update pip for Local Voices (exit code $LASTEXITCODE)." }
    & $speechPython -m pip install 'faster-whisper==1.2.1'
    if ($LASTEXITCODE -ne 0) { throw "could not install Faster Whisper (exit code $LASTEXITCODE)." }
    & $speechPython -c "import faster_whisper"
    if ($LASTEXITCODE -ne 0) { throw 'Faster Whisper could not be imported after installation.' }
  } catch {
    $speechError = $_.Exception.Message
    Write-BootstrapLog "Local transcription setup unavailable: $speechError"
  }
  return @{ Python = if ([string]::IsNullOrEmpty($speechError)) { $speechPython } else { '' }; Error = $speechError }
}

function Test-LocalVoiceClone {
  param([string]$VoicePython)
  if (-not (Test-Path $VoicePython)) { return $false }
  try {
    & $VoicePython -c "from voice_clone_module import VoiceCloner; from chatterbox.tts import ChatterboxTTS" 2>$null
    return $LASTEXITCODE -eq 0
  } catch {}
  return $false
}

function Enable-LocalVoiceClone {
  param([string]$PythonLauncher, [string]$RuntimePath, [string]$VoicePackage)
  $voiceHome = Join-Path $RuntimePath 'local-voices'
  $voicePython = Join-Path $voiceHome 'Scripts\python.exe'
  $voiceError = ''
  try {
    if (-not (Get-Command git.exe -ErrorAction SilentlyContinue)) { throw 'Git is required to install the pinned Chatterbox dependencies.' }
    if (-not (Test-Path $VoicePackage)) { throw 'Eva voice adapter package is missing from the installed application.' }
    & $PythonLauncher -3.11 -m venv $voiceHome
    if ($LASTEXITCODE -ne 0) { throw "could not create the Local Voices environment (exit code $LASTEXITCODE)." }
    & $voicePython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "could not update pip for Local Voices (exit code $LASTEXITCODE)." }
    $voicePackages = @(
      'torch==2.10.0', 'torchaudio==2.10.0', 'faster-whisper==1.2.1', 'soundfile==0.14.0', 'librosa==0.11.0',
      'transformers==5.5.0', 'accelerate==1.14.0', 'bitsandbytes==0.49.2', 'numpy==1.26.4', 's3tokenizer==0.3.0',
      'diffusers==0.38.0', 'resemble-perth @ git+https://github.com/resemble-ai/Perth.git@ce86c49d029f42272c1902eccb675556b9ed2330',
      'conformer==0.3.2', 'safetensors==0.8.0', 'spacy-pkuseg==1.0.1', 'pykakasi==2.3.0', 'pyloudnorm==0.2.0',
      'omegaconf==2.3.1', 'gradio==6.16.0'
    )
    & $voicePython -m pip install @voicePackages
    if ($LASTEXITCODE -ne 0) { throw "could not install Local Voices dependencies (exit code $LASTEXITCODE)." }
    & $voicePython -m pip install --no-deps --force-reinstall 'chatterbox-tts @ git+https://github.com/resemble-ai/chatterbox.git@5de7a54aa4e5e2baadb0182dde554908b48b85c2'
    if ($LASTEXITCODE -ne 0) { throw "could not install Chatterbox (exit code $LASTEXITCODE)." }
    & $voicePython -m pip install --no-deps --force-reinstall $VoicePackage
    if ($LASTEXITCODE -ne 0) { throw "could not install Eva's voice adapter (exit code $LASTEXITCODE)." }
    if (-not (Test-LocalVoiceClone -VoicePython $voicePython)) { throw 'Chatterbox could not be imported after installation.' }
  } catch {
    $voiceError = $_.Exception.Message
    Write-BootstrapLog "Local Voices cloning setup unavailable: $voiceError"
  }
  return @{ Python = if ([string]::IsNullOrEmpty($voiceError)) { $voicePython } else { '' }; Error = $voiceError }
}

try {
  $python = Get-Python312
  if (-not $python) {
    Install-WingetPackage 'Python.Python.3.12'
    $python = Get-Python312
  }
  if (-not $python) { throw 'Python 3.12 was not available after installation.' }

  $node = Get-Node24
  if (-not $node) {
    Install-WingetPackage 'OpenJS.NodeJS.LTS'
    $node = Get-Node24
  }
  if (-not $node) { throw 'Node.js 24 or newer was not available after installation.' }

  $npm = Join-Path (Split-Path $node -Parent) 'npm.cmd'
  if (-not (Test-Path $npm)) { throw "npm.cmd was not found beside $node." }
  $copilotPrefix = Join-Path $runtimeRoot 'copilot'
  $copilot = Join-Path $copilotPrefix 'copilot.cmd'
  if (-not (Test-Path $copilot)) {
    Write-BootstrapLog 'Installing GitHub Copilot CLI in Eva runtime storage.'
    & $npm install --global --prefix $copilotPrefix '@github/copilot'
    if ($LASTEXITCODE -ne 0) { throw "npm failed to install GitHub Copilot CLI (exit code $LASTEXITCODE)." }
  }
  if (-not (Test-Path $copilot)) { throw 'GitHub Copilot CLI was not installed.' }
  $copilotNode = Join-Path $copilotPrefix 'node.exe'
  if (-not (Test-Path $copilotNode)) {
    Copy-Item -Path $node -Destination $copilotNode
  }

  $speechPython = Join-Path $runtimeRoot 'speech\Scripts\python.exe'
  $speechReady = $false
  if (Test-Path $speechPython) {
    & $speechPython -c "import faster_whisper" 2>$null
    $speechReady = $LASTEXITCODE -eq 0
  }
  if (-not $speechReady) {
    Write-BootstrapLog 'Installing Local Voices transcription runtime.'
    $speechSetup = Enable-LocalTranscription -PythonLauncher $python -RuntimePath $runtimeRoot
    $speechPython = $speechSetup.Python
    $speechError = $speechSetup.Error
  } else {
    $speechError = ''
  }

  $voicePython = Join-Path $runtimeRoot 'local-voices\Scripts\python.exe'
  $voiceCloneReady = Test-LocalVoiceClone -VoicePython $voicePython
  if (-not $voiceCloneReady) {
    $python311 = Get-Python311
    if (-not $python311) {
      Install-WingetPackage 'Python.Python.3.11'
      $python311 = Get-Python311
    }
    if (-not $python311) {
      $voiceError = 'Python 3.11 was not available after installation.'
      Write-BootstrapLog "Local Voices cloning setup unavailable: $voiceError"
      $voicePython = ''
    } else {
      $voicePackage = Join-Path (Split-Path $PSScriptRoot -Parent) 'app\tools\voice_clone_module'
      if (-not (Test-Path $voicePackage)) {
        $voicePackage = Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'tools\voice_clone_module'
      }
      Write-BootstrapLog 'Installing Local Voices Chatterbox runtime. This can download several gigabytes on first install.'
      $voiceSetup = Enable-LocalVoiceClone -PythonLauncher $python311 -RuntimePath $runtimeRoot -VoicePackage $voicePackage
      $voicePython = $voiceSetup.Python
      $voiceError = $voiceSetup.Error
    }
  } else {
    $voiceError = ''
  }

  $localSpeechPython = if ($voicePython) { $voicePython } else { $speechPython }

  @{ python = $python; pythonArgs = @('-3.12'); node = $node; copilot = $copilot; localSpeechPython = $localSpeechPython; localSpeechError = $speechError; localVoiceClonePython = $voicePython; localVoiceCloneError = $voiceError; updatedAt = (Get-Date -Format o) } |
    ConvertTo-Json | Set-Content -Path $manifestPath -Encoding utf8
  Write-BootstrapLog 'Eva runtime prerequisites are ready.'

  if ($LaunchLogin) {
    Write-BootstrapLog 'Opening GitHub Copilot sign-in terminal.'
    Start-Process -FilePath 'cmd.exe' -ArgumentList @('/k', ('"{0}"' -f $copilot))
  }
} catch {
  @{ error = $_.Exception.Message; updatedAt = (Get-Date -Format o) } |
    ConvertTo-Json | Set-Content -Path $manifestPath -Encoding utf8
  Write-BootstrapLog "Runtime bootstrap failed: $($_.Exception.Message)"
}

exit 0
