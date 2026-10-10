$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$version = (Get-Content VERSION -Raw).Trim()
python -m pip install -r requirements_app.txt -r requirements_whisper.txt imageio-ffmpeg
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
python prepare_build.py
if ($LASTEXITCODE -ne 0) { throw 'Metadata preparation failed' }
$env:ZUCKER_APP_VERSION = $version
$env:ZUCKER_FFMPEG_BINARY = (python -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())").Trim()
# Verify the same frozen Python archive in a console build first, so missing
# DLLs/imports produce an actionable traceback instead of a hidden modal.
$env:ZUCKER_BUILD_CONSOLE = '1'
python -m PyInstaller --noconfirm zucker_mixer.spec
if ($LASTEXITCODE -ne 0) { throw 'Diagnostic PyInstaller build failed' }
$exe = "$PWD/dist/ZuckerMixer/ZuckerMixer.exe"
$helper = Start-Process -FilePath $exe -ArgumentList '--update-helper', '--self-check' -PassThru
if (-not $helper.WaitForExit(30000)) { Stop-Process -Id $helper.Id -Force; throw 'Frozen updater helper timed out' }
if ($helper.ExitCode -ne 0) { throw 'Frozen updater helper failed' }
$process = Start-Process -FilePath $exe -ArgumentList '--self-check' -RedirectStandardOutput "$PWD/build/self-check.stdout.log" -RedirectStandardError "$PWD/build/self-check.stderr.log" -PassThru
if (-not $process.WaitForExit(120000)) {
  Stop-Process -Id $process.Id -Force
  Get-Content build/self-check.*.log
  throw 'Frozen console self-check timed out'
}
Get-Content build/self-check.*.log
if ($process.ExitCode -ne 0) { throw 'Frozen console self-check failed' }
$env:ZUCKER_BUILD_CONSOLE = '0'
python -m PyInstaller --noconfirm zucker_mixer.spec
if ($LASTEXITCODE -ne 0) { throw 'Windowed PyInstaller build failed' }
$process = Start-Process -FilePath $exe -ArgumentList '--self-check' -PassThru
if (-not $process.WaitForExit(120000)) {
  Stop-Process -Id $process.Id -Force
  throw 'Frozen windowed self-check timed out'
}
if ($process.ExitCode -ne 0) { throw 'Frozen windowed self-check failed' }
Copy-Item README.md,LICENSE,THIRD_PARTY_NOTICES.md dist/ZuckerMixer
Compress-Archive -Path dist/ZuckerMixer -DestinationPath "dist/ZuckerMixer-$version-Windows.zip" -Force

# Offline, signed Microsoft runtime: end users do not download prerequisites.
$runtime = "$PWD/build/MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
Invoke-WebRequest 'https://go.microsoft.com/fwlink/p/?LinkId=2124701' -OutFile $runtime
$signature = Get-AuthenticodeSignature $runtime
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Microsoft Corporation') {
  throw 'WebView2 installer signature is not valid Microsoft code'
}
$compiler = (Get-Command makensis.exe -ErrorAction SilentlyContinue).Source
if (-not $compiler) { $compiler = "${env:ProgramFiles(x86)}/NSIS/makensis.exe" }
& $compiler "/DVERSION=$version" packaging/windows-installer.nsi
if ($LASTEXITCODE -ne 0) { throw 'Single executable installer build failed' }
# Emulate a browser download: only the outer installer has Mark of the Web.
$installer = "$PWD/dist/ZuckerMixer-$version-Windows.exe"
Set-Content -Path $installer -Stream Zone.Identifier -Value "[ZoneTransfer]`r`nZoneId=3"
$testRoot = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { $env:TEMP }
$testDir = Join-Path $testRoot 'ZuckerMixer-installed-check'
$process = Start-Process $installer -ArgumentList '/NOLAUNCH', "/D=$testDir" -RedirectStandardOutput "$PWD/build/installer.stdout.log" -RedirectStandardError "$PWD/build/installer.stderr.log" -PassThru
if (-not $process.WaitForExit(180000)) {
  Stop-Process -Id $process.Id -Force
  throw 'Installer / Windows UI check timed out'
}
if ($process.ExitCode -ne 0) { throw 'Installed Windows UI check failed' }
$blocked = Get-Item "$testDir/_internal/pythonnet/runtime/Python.Runtime.dll" -Stream Zone.Identifier -ErrorAction SilentlyContinue
if ($blocked) { throw 'Installed CLR assembly inherited Mark of the Web' }
