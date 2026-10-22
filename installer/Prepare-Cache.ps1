param([string]$GamePath, [ValidateRange(1,32)][int]$Threads = 4)
. (Join-Path $PSScriptRoot 'Common.ps1')
$game = Get-GameDirectory $GamePath
Assert-GameClosed $game
$module = Get-ModulePath $game
$executable = Join-Path $module 'Local_Translator_RU.exe'
if (-not (Test-Path -LiteralPath $executable)) { throw 'Install the translator first.' }
$progress = Join-Path $module 'precache\progress.json'
$arguments = '--precache --game-dir "' + $game + '" --threads ' + $Threads
$process = Start-Process -FilePath $executable -ArgumentList $arguments -PassThru -WindowStyle Hidden
do {
    Start-Sleep -Seconds 2
    if (Test-Path -LiteralPath $progress) {
        try {
            $state = Get-Content -LiteralPath $progress -Raw | ConvertFrom-Json
            $percent = if ($state.total -gt 0) { [int](100 * $state.done / $state.total) } else { 0 }
            Write-Progress -Activity 'Preparing persistent Russian cache' -Status ($state.phase + ' ' + $state.done + '/' + $state.total) -PercentComplete $percent
        } catch {}
    }
    $process.Refresh()
} until ($process.HasExited)
$process.WaitForExit()
Write-Progress -Activity 'Preparing persistent Russian cache' -Completed
$state = if (Test-Path -LiteralPath $progress) { Get-Content -LiteralPath $progress -Raw | ConvertFrom-Json } else { $null }
if ($process.ExitCode -ne 0 -or -not $state -or $state.phase -ne 'complete') {
    throw "Precache did not complete. $($state.error) See $module\precache\precache.log"
}
Write-Host ("Ready. Local cache: " + $state.local_cache_entries + '; game cache: ' + $state.game_cache_entries) -ForegroundColor Green
Write-Host 'The game archive was read only. Previously unavailable server text may still require translation.'
