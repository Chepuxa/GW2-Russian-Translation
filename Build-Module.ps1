param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$sources = Join-Path $PSScriptRoot 'components\local-translator'
$artifacts = Join-Path $PSScriptRoot '.artifacts'
$editorAsset = (Join-Path $sources 'contribution_editor.js')+';.'
& $Python -m PyInstaller --noconfirm --noconsole --onedir --name Local_Translator_RU --paths (Join-Path $sources 'source\src') --paths (Join-Path $PSScriptRoot 'tools') --hidden-import reviewed_setup --hidden-import catalog --collect-all ctranslate2 --add-data ((Join-Path $sources 'glossary_ru.default.json')+';.') --add-data ((Join-Path $sources 'contribution_ui.html')+';.') --add-data $editorAsset --distpath (Join-Path $artifacts 'module-dist') --workpath (Join-Path $artifacts 'module-work') --specpath (Join-Path $artifacts 'module-work') (Join-Path $sources 'ru_launcher.py')
if ($LASTEXITCODE -ne 0) { throw 'Module build failed. Install components/local-translator/requirements-build.txt into Python 3.12 x64.' }
$destination = Join-Path $artifacts 'runtime\game\addons\text_translator\modules\local_translator_ru'
[IO.Directory]::CreateDirectory($destination) | Out-Null
Copy-Item -Path (Join-Path $artifacts 'module-dist\Local_Translator_RU\*') -Destination $destination -Recurse -Force
Copy-Item -LiteralPath (Join-Path $sources 'source\LICENSE') -Destination (Join-Path $destination 'UPSTREAM-LICENSE.txt') -Force
[IO.Directory]::CreateDirectory((Join-Path $destination 'RU')) | Out-Null
Copy-Item -LiteralPath (Join-Path $sources 'glossary_ru.default.json') -Destination (Join-Path $destination 'RU\glossary_ru.json') -Force
$fontDestination = Join-Path $artifacts 'runtime\game\glyphCore'
[IO.Directory]::CreateDirectory($fontDestination) | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'assets\fonts\AlegreyaSans-Medium.ttf') -Destination (Join-Path $fontDestination 'cyrillic.ttf') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'assets\fonts\OFL.txt') -Destination (Join-Path $fontDestination 'AlegreyaSans-OFL.txt') -Force
& $Python (Join-Path $PSScriptRoot 'tools\release.py') lock
if ($LASTEXITCODE -ne 0) { throw 'Runtime pinning failed' }
