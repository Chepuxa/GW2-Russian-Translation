param([string]$GamePath)
. (Join-Path $PSScriptRoot 'Common.ps1')
$game = Get-GameDirectory $GamePath
$module = Get-ModulePath $game
$executable = Join-Path $module 'Local_Translator_RU.exe'
if (-not (Test-Path -LiteralPath $executable)) { throw 'Install the translator first.' }
$arguments = '--contribute --module-dir "' + $module + '"'
$project = Split-Path -Parent $PSScriptRoot
$arguments += ' --project-root "' + $project + '"'
$package = if (Test-Path -LiteralPath (Join-Path $project 'manifest.json')) { $project } else { Join-Path $project 'dist\GW2-Russian-Translation' }
$packagedModule = Join-Path $package 'payload\game\addons\text_translator\modules\local_translator_ru'
# The viewer can run from the new package while the game still uses an older module.
# Actual in-game discovery starts after installation/restart; its status is shown in the panel.
if (Test-Path -LiteralPath (Join-Path $packagedModule 'Local_Translator_RU.exe')) {
    $executable = Join-Path $packagedModule 'Local_Translator_RU.exe'
    $arguments += ' --known-index "' + (Join-Path $packagedModule 'community\known-strings.db') + '"'
}
Start-Process -FilePath $executable -ArgumentList $arguments -WindowStyle Hidden | Out-Null
Write-Host 'The translation workshop opens in your browser. The game may remain running.'
