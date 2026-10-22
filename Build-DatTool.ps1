param([string]$Cargo = 'cargo')
$ErrorActionPreference = 'Stop'
$source = Join-Path $PSScriptRoot 'components\glyphcore\dat-tool'
& $Cargo build --release --locked --manifest-path (Join-Path $source 'Cargo.toml')
if ($LASTEXITCODE -ne 0) { throw 'Archive reader build failed. Rust x64 MSVC and its C++ build tools are required.' }
$destination = Join-Path $PSScriptRoot '.artifacts\runtime\game\addons\text_translator\modules\local_translator_ru\tools'
[IO.Directory]::CreateDirectory($destination) | Out-Null
Copy-Item -LiteralPath (Join-Path $source 'target\release\gw2-dat-tool.exe') -Destination (Join-Path $destination 'gw2-dat-tool.exe') -Force
