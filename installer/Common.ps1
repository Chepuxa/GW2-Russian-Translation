$ErrorActionPreference = 'Stop'

function Get-PackageHash([string]$LiteralPath, [string]$Algorithm = 'SHA256') {
    if ($Algorithm -ne 'SHA256') { throw 'Only SHA256 is supported' }
    $stream = [IO.File]::OpenRead($LiteralPath)
    $hasher = [Security.Cryptography.SHA256]::Create()
    try { $hash = ([BitConverter]::ToString($hasher.ComputeHash($stream))).Replace('-','') }
    finally { $hasher.Dispose(); $stream.Dispose() }
    return [pscustomobject]@{Hash=$hash;Path=$LiteralPath;Algorithm='SHA256'}
}

function Get-GameDirectory([string]$GamePath) {
    if ([string]::IsNullOrWhiteSpace($GamePath)) {
        $nearby = $PSScriptRoot
        for ($i = 0; $i -lt 5 -and $nearby; $i++) {
            if (Test-Path -LiteralPath (Join-Path $nearby 'Gw2-64.exe')) { $GamePath = $nearby; break }
            $nearby = Split-Path -Parent $nearby
        }
        if (-not $GamePath) { $GamePath = Read-Host 'Path to the folder containing Gw2-64.exe' }
    }
    $resolved = (Resolve-Path -LiteralPath $GamePath.Trim('"')).Path
    foreach ($name in @('Gw2-64.exe', 'Gw2.dat')) {
        if (-not (Test-Path -LiteralPath (Join-Path $resolved $name) -PathType Leaf)) {
            throw "Missing $name in $resolved"
        }
    }
    return $resolved.TrimEnd('\')
}

function Assert-GameClosed([string]$GameDirectory) {
    foreach ($name in @('Gw2-64', 'Local_Translator_RU')) {
        foreach ($process in @(Get-Process -Name $name -ErrorAction SilentlyContinue)) {
            $path = $null
            try { $path = $process.Path } catch {}
            if (-not $path -or $path.StartsWith($GameDirectory + '\', [StringComparison]::OrdinalIgnoreCase)) {
                throw 'Close Guild Wars 2, its Russian translator, and the workshop using Close panel, then run this command again.'
            }
        }
    }
}

function Write-Utf8([string]$Path, [string]$Text) {
    $parent = Split-Path -Parent $Path
    [IO.Directory]::CreateDirectory($parent) | Out-Null
    [IO.File]::WriteAllText($Path, $Text, [Text.UTF8Encoding]::new($false))
}

function Set-IniValue([string]$Path, [string]$Key, [string]$Value) {
    $content = if (Test-Path -LiteralPath $Path) { [IO.File]::ReadAllText($Path) } else { '' }
    $pattern = '(?m)^\s*' + [regex]::Escape($Key) + '\s*=.*$'
    if ([regex]::IsMatch($content, $pattern)) { $content = [regex]::Replace($content, $pattern, ("$Key=$Value".Replace('$','$$'))) }
    else { $content = $content.TrimEnd() + "`r`n$Key=$Value`r`n" }
    Write-Utf8 $Path $content
}

function Get-ModulePath([string]$GameDirectory) {
    return Join-Path $GameDirectory 'addons\text_translator\modules\local_translator_ru'
}

function Get-RussianSettings([bool]$ShowTranslated = $true, $Profile = $null) {
    $bytes = [Text.Encoding]::UTF8.GetBytes('local_translator_ru') -join ', '
    $show = $ShowTranslated.ToString().ToLowerInvariant()
    $pause = if ($Profile) { $Profile.translator.pause_refreshes.ToString().ToLowerInvariant() } else { 'true' }
    $seconds = if ($Profile) { [int]$Profile.translator.refresh_debounce_secs } else { 1 }
    return "active_module = { Windows = [$bytes] }`r`nshow_translated = $show`r`npause_refreshes = $pause`r`npause_translations = false`r`n`r`n[refresh_debounce]`r`nsecs = $seconds`r`nnanos = 0`r`n"
}

function Set-TranslationVisibility([string]$Settings, [bool]$Enabled) {
    if (-not (Test-Path -LiteralPath $Settings)) { throw 'Text Translator settings are missing. Run Install first.' }
    $text = [IO.File]::ReadAllText($Settings)
    $value = $Enabled.ToString().ToLowerInvariant()
    if ($text -match '(?m)^show_translated\s*=') {
        $text = [regex]::Replace($text, '(?m)^show_translated\s*=.*$', "show_translated = $value")
    } else { $text = "show_translated = $value`r`n" + $text }
    Write-Utf8 $Settings $text
}
