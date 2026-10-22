param([string]$GamePath, [string]$CachePath)
. (Join-Path $PSScriptRoot 'Common.ps1')
$game = Get-GameDirectory $GamePath
Assert-GameClosed $game
if (-not $CachePath) { $CachePath = Read-Host 'Path to GW2-RU-cache ZIP' }
$cache = (Resolve-Path -LiteralPath $CachePath.Trim('"')).Path
$module = Get-ModulePath $game
$result = Join-Path $module 'precache\import-result.json'
$arguments = '--precache --game-dir "' + $game + '" --import-cache "' + $cache + '" --progress-file "' + $result + '"'
$process = Start-Process -FilePath (Join-Path $module 'Local_Translator_RU.exe') -ArgumentList $arguments -WindowStyle Hidden -PassThru -Wait
$state = if (Test-Path -LiteralPath $result) { Get-Content -LiteralPath $result -Raw | ConvertFrom-Json } else { $null }
if ($process.ExitCode -ne 0 -or -not $state -or $state.phase -ne 'complete') { throw "Cache import failed. $($state.error)" }
Write-Host ("Cache merged. Local entries: " + $state.local_cache_entries + '; new game entries: ' + $state.game_rows_added) -ForegroundColor Green
