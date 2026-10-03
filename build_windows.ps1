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
