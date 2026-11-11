# Builds Wavegrab.exe (PyInstaller) and the Windows installer (Inno Setup).
#   powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$version = (Select-String -Path wavegrab\app.py -Pattern 'VERSION = "(.+)"').Matches[0].Groups[1].Value
Write-Host "== Wavegrab $version ==" -ForegroundColor Magenta

# 1. Python deps
python -m pip install -q -r requirements.txt pyinstaller

# 2. Icon
python tools\make_icon.py

# 3. Bundled tools: ffmpeg (essentials build) and deno
$bin = "vendor\bin"
New-Item -ItemType Directory -Force $bin | Out-Null
if (-not (Test-Path "$bin\ffmpeg.exe")) {
    Write-Host "Downloading ffmpeg..."
    $zip = "vendor\ffmpeg.zip"
    Invoke-WebRequest "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" -OutFile $zip
    Expand-Archive $zip -DestinationPath vendor\ffmpeg -Force
    # ffprobe isn't needed by Wavegrab (saves ~100 MB)
    Get-ChildItem vendor\ffmpeg -Recurse -Include ffmpeg.exe | Copy-Item -Destination $bin
    Remove-Item $zip, vendor\ffmpeg -Recurse -Force
}
if (-not (Test-Path "$bin\deno.exe")) {
    Write-Host "Downloading deno..."
    $zip = "vendor\deno.zip"
    Invoke-WebRequest "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip" -OutFile $zip
    Expand-Archive $zip -DestinationPath $bin -Force
    Remove-Item $zip
}

# 4. The .exe
New-Item -ItemType Directory -Force build | Out-Null
(Get-Content tools\version_info.txt -Raw).Replace("{VERSION}", $version).Replace("{VTUPLE}", ($version.Replace(".", ", ") + ", 0")) |
    Set-Content build\version_info.txt -Encoding utf8
python -m PyInstaller --noconfirm --clean --windowed --name Wavegrab `
    --icon wavegrab\assets\icon.ico --version-file build\version_info.txt `
    --add-data "wavegrab\assets;assets" `
    --collect-all customtkinter --collect-all yt_dlp_ejs --collect-submodules yt_dlp `
    run.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
Copy-Item $bin dist\Wavegrab\bin -Recurse -Force

# 5. Installer
$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup not found: winget install JRSoftware.InnoSetup" }
& $iscc /Qp "/DMyAppVersion=$version" installer.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
Write-Host "Done: dist\Wavegrab-Setup-$version.exe" -ForegroundColor Green
