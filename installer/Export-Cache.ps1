param([string]$GamePath, [string]$OutputPath)
. (Join-Path $PSScriptRoot 'Common.ps1')
$game = Get-GameDirectory $GamePath
Assert-GameClosed $game
if (-not $OutputPath) { $OutputPath = Join-Path $PSScriptRoot ('GW2-RU-cache-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.zip') }
$output = [IO.Path]::GetFullPath($OutputPath)
if (Test-Path -LiteralPath $output) { throw "Output already exists: $output" }
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$module = Get-ModulePath $game
$zip = [IO.Compression.ZipFile]::Open($output, [IO.Compression.ZipArchiveMode]::Create)
try {
    foreach ($relative in @('cache.db','RU\cache_ru.jsonl','RU\cache_ru.glossary.json','RU\glossary_ru.json','RU\map_ru.db','RU\patch_ru.db')) {
        $path = Join-Path $module $relative
        if (Test-Path -LiteralPath $path) {
            [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip,$path,$relative.Replace('\','/'),[IO.Compression.CompressionLevel]::Optimal) | Out-Null
        }
    }
} finally { $zip.Dispose() }
Write-Host "Cache exported: $output" -ForegroundColor Green
