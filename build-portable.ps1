$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$version = '1.0.3'
$name = 'VRAM-Monitor'
$dist = Join-Path $PSScriptRoot 'dist'
$build = Join-Path $PSScriptRoot 'build'
$release = Join-Path $PSScriptRoot 'release'
$outDir = Join-Path $release "$name-v$version-portable"
$zip = Join-Path $release "$name-v$version-portable.zip"

New-Item -ItemType Directory -Path $release -Force | Out-Null

python -m PyInstaller `
  --noconfirm --clean --onefile --windowed `
  --name $name `
  --icon "$PSScriptRoot\icon.ico" `
  --add-data "$PSScriptRoot\web;web" `
  --version-file "$PSScriptRoot\version_info.txt" `
  --distpath $dist `
  --workpath $build `
  --specpath $build `
  app.py

if ($LASTEXITCODE -ne 0) {
  throw "PyInstaller failed with exit code $LASTEXITCODE"
}

$exe = Join-Path $dist "$name.exe"
if (-not (Test-Path $exe)) {
  throw "missing exe: $exe"
}

if (Test-Path $outDir) { Remove-Item $outDir -Recurse -Force }
New-Item -ItemType Directory -Path $outDir | Out-Null
Copy-Item $exe (Join-Path $outDir "$name.exe")
Copy-Item (Join-Path $PSScriptRoot 'PORTABLE.txt') (Join-Path $outDir 'PORTABLE.txt')

if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $outDir -DestinationPath $zip -CompressionLevel Optimal

Get-Item $exe, $zip | Select-Object FullName, Length
