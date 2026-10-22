param([string]$GamePath)
. (Join-Path $PSScriptRoot 'Common.ps1')
$game = Get-GameDirectory $GamePath
Assert-GameClosed $game
Set-TranslationVisibility (Join-Path $game 'addons\text_translator\settings.toml') $false
Write-Host 'Translation display disabled. Your caches and font are preserved.' -ForegroundColor Green
