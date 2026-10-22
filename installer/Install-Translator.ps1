param(
    [string]$GamePath,
    [int]$GlyphSize = 0,
    [string]$PackagePath
)
. (Join-Path $PSScriptRoot 'Common.ps1')
$game = Get-GameDirectory $GamePath
Assert-GameClosed $game
$project = Split-Path -Parent $PSScriptRoot
if (-not $PackagePath) {
    $PackagePath = if (Test-Path -LiteralPath (Join-Path $project 'manifest.json')) { $project } else { Join-Path $project 'dist\GW2-Russian-Translation' }
}
$package = (Resolve-Path -LiteralPath $PackagePath).Path
$payload = Join-Path $package 'payload'
$manifest = Get-Content -LiteralPath (Join-Path $package 'manifest.json') -Raw | ConvertFrom-Json
if ($GlyphSize -ne 0 -and ($GlyphSize -lt 60 -or $GlyphSize -gt 109)) { throw 'GlyphSize must be 60..109' }

# Verify the entire package before making any changes to the game installation.
foreach ($entry in $manifest.files) {
    $path = [IO.Path]::GetFullPath((Join-Path $payload $entry.path))
    if (-not $path.StartsWith($payload + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid package path' }
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing package file: $($entry.path)" }
    if ((Get-PackageHash -LiteralPath $path -Algorithm SHA256).Hash -ine $entry.sha256) {
        throw "Package integrity check failed: $($entry.path)"
    }
}
$profile = Get-Content -LiteralPath (Join-Path $payload 'profile.json') -Raw | ConvertFrom-Json
if ($profile.format -ne 1) { throw 'Unsupported settings profile' }
if ($GlyphSize -eq 0) { $GlyphSize = [int]$profile.glyph.glyph_size }

$rootFiles = @('d3d11.dll', 'version.dll', 'addons\text_translator.dll')
foreach ($relative in $rootFiles) {
    $existing = Join-Path $game $relative
    $incoming = Join-Path $payload ('game\' + $relative)
    if ((Test-Path -LiteralPath $existing) -and
        (Get-PackageHash -LiteralPath $existing).Hash -ine (Get-PackageHash -LiteralPath $incoming).Hash) {
        throw "An existing component differs from this tested version: $existing. It was left untouched."
    }
}

$stateDir = Join-Path $game 'addons\text_translator\ru_setup'
$backup = Join-Path $stateDir ('backups\' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
[IO.Directory]::CreateDirectory($backup) | Out-Null
$changed = New-Object 'Collections.Generic.List[string]'

function Copy-InstalledFile([string]$Source, [string]$Relative) {
    $destination = Join-Path $game $Relative
    if (Test-Path -LiteralPath $destination) {
        if ((Get-PackageHash -LiteralPath $destination).Hash -eq (Get-PackageHash -LiteralPath $Source).Hash) { return }
        $saved = Join-Path $backup $Relative
        [IO.Directory]::CreateDirectory((Split-Path -Parent $saved)) | Out-Null
        Copy-Item -LiteralPath $destination -Destination $saved
    }
    [IO.Directory]::CreateDirectory((Split-Path -Parent $destination)) | Out-Null
    Copy-Item -LiteralPath $Source -Destination $destination -Force
    $changed.Add($Relative)
}

$gamePayload = Join-Path $payload 'game'
foreach ($entry in $manifest.files) {
    if (-not $entry.path.StartsWith('game\', [StringComparison]::OrdinalIgnoreCase)) { continue }
    $relative = $entry.path.Substring(5)
    $source = Join-Path $gamePayload $relative
    # Keep a user's edited glossary and persistent caches during upgrades/reinstallation.
    if ($relative -eq 'addons\text_translator\modules\local_translator_ru\RU\glossary_ru.json' -and
        (Test-Path -LiteralPath (Join-Path $game $relative))) { continue }
    Copy-InstalledFile $source $relative
}

$settingsRelative = 'addons\text_translator\settings.toml'
$settings = Join-Path $game $settingsRelative
if (Test-Path -LiteralPath $settings) {
    $saved = Join-Path $backup $settingsRelative
    [IO.Directory]::CreateDirectory((Split-Path -Parent $saved)) | Out-Null
    Copy-Item -LiteralPath $settings -Destination $saved
    $text = [IO.File]::ReadAllText($settings)
    $bytes = [Text.Encoding]::UTF8.GetBytes('local_translator_ru') -join ', '
    # Existing settings may use either inline or multiline TOML for the active module.
    $text = [regex]::Replace($text, '(?ms)^active_module\s*=\s*\{.*?\}\s*\r?\n?', '')
    $text = [regex]::Replace($text, '(?ms)^\[active_module\]\s*.*?(?=^\[|\z)', '')
    $text = "active_module = { Windows = [$bytes] }`r`n" + $text
    Write-Utf8 $settings $text
    Set-TranslationVisibility $settings $true
} else { Write-Utf8 $settings (Get-RussianSettings $true $profile) }

$glyph = Join-Path $game 'glyphCore\config.ini'
if (Test-Path -LiteralPath $glyph) {
    $saved = Join-Path $backup 'glyphCore\config.ini'
    [IO.Directory]::CreateDirectory((Split-Path -Parent $saved)) | Out-Null
    Copy-Item -LiteralPath $glyph -Destination $saved
}
foreach ($property in $profile.glyph.PSObject.Properties) {
    $value = if ($property.Value -is [bool]) { $property.Value.ToString().ToLowerInvariant() } else { [string]$property.Value }
    Set-IniValue $glyph $property.Name $value
}
Set-IniValue $glyph 'glyph_size' ([string]$GlyphSize)
Set-IniValue $glyph 'glyph_font' (Join-Path $game 'glyphCore\cyrillic.ttf')

$module = Get-ModulePath $game
$bundledCache = Join-Path $payload 'bootstrap-cache.zip'
if (Test-Path -LiteralPath $bundledCache) {
    $cacheReport = Join-Path $stateDir 'bundled-cache-install.json'
    $cacheArguments = '--precache --game-dir "' + $game + '" --import-cache "' + $bundledCache + '" --progress-file "' + $cacheReport + '"'
    $cacheProcess = Start-Process -FilePath (Join-Path $module 'Local_Translator_RU.exe') -ArgumentList $cacheArguments -WindowStyle Hidden -PassThru -Wait
    $cacheState = if (Test-Path -LiteralPath $cacheReport) { Get-Content -LiteralPath $cacheReport -Raw | ConvertFrom-Json } else { $null }
    if ($cacheProcess.ExitCode -ne 0 -or -not $cacheState -or $cacheState.phase -ne 'complete') {
        if ($cacheState -and $cacheState.error -match 'different Russian glossary|different markup-protection rules') {
            Write-Warning ('Bundled cache skipped to preserve your custom glossary. ' + $cacheState.error)
        } else { throw "Bundled cache install failed: $($cacheState.error). Backups: $backup" }
    } else { Write-Host ("Bundled machine cache: " + $cacheState.local_cache_entries + ' source lines') }
}
$reviewReport = Join-Path $stateDir 'reviewed-install.json'
$arguments = '--install-reviewed --game-dir "' + $game + '" --catalog "' + (Join-Path $module 'community') + '" --report "' + $reviewReport + '"'
if (Test-Path -LiteralPath $reviewReport) { Copy-Item -LiteralPath $reviewReport -Destination (Join-Path $backup 'reviewed-install.json') }
$process = Start-Process -FilePath (Join-Path $module 'Local_Translator_RU.exe') -ArgumentList $arguments -WindowStyle Hidden -PassThru -Wait
$reviewed = if (Test-Path -LiteralPath $reviewReport) { Get-Content -LiteralPath $reviewReport -Raw | ConvertFrom-Json } else { $null }
if ($process.ExitCode -ne 0 -or -not $reviewed -or $reviewed.phase -ne 'complete') { throw "Reviewed catalog install failed: $($reviewed.error). Backups: $backup" }
Write-Utf8 (Join-Path $stateDir 'install.json') (@{
    version=$manifest.version; installed=(Get-Date).ToString('o'); game=$game;
    backup=$backup; changed_files=$changed.ToArray(); glyph_size=$GlyphSize;
    reviewed=$reviewed.installed_reviewed
} | ConvertTo-Json -Depth 5)
Write-Host "Installed Russian translator: $game" -ForegroundColor Green
Write-Host 'Start the game in English. Open Nexus using its top icon. Show/hide translations: period key (.).'
Write-Host "Backups: $backup"
