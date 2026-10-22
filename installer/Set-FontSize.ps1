param([string]$GamePath, [ValidateRange(60,109)][int]$Size = 90)
. (Join-Path $PSScriptRoot 'Common.ps1')
$game = Get-GameDirectory $GamePath
Assert-GameClosed $game
Set-IniValue (Join-Path $game 'glyphCore\config.ini') 'glyph_size' ([string]$Size)
Write-Host "Cyrillic glyph size set to $Size percent. Start the game to apply it."
