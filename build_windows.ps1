$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$version = (Get-Content VERSION -Raw).Trim()
python -m pip install -r requirements_app.txt -r requirements_whisper.txt imageio-ffmpeg
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
python prepare_build.py
if ($LASTEXITCODE -ne 0) { throw 'Metadata preparation failed' }
$env:ZUCKER_APP_VERSION = $version
$env:ZUCKER_FFMPEG_BINARY = (python -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())").Trim()
python -m PyInstaller --noconfirm zucker_mixer.spec
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }
$process = Start-Process -FilePath "$PWD/dist/ZuckerMixer/ZuckerMixer.exe" -ArgumentList '--self-check' -Wait -PassThru
if ($process.ExitCode -ne 0) { throw 'Frozen self-check failed' }
Copy-Item README.md,LICENSE,THIRD_PARTY_NOTICES.md dist/ZuckerMixer
Compress-Archive -Path dist/ZuckerMixer -DestinationPath "dist/ZuckerMixer-$version-Windows.zip" -Force
