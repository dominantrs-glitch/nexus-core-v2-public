param([ValidateSet('Install','Remove')][string]$Action = 'Install')
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$python = Join-Path $repo '.venv\Scripts\pythonw.exe'
if (!(Test-Path -LiteralPath $python)) { throw 'Python runtime missing' }
$shell = New-Object -ComObject WScript.Shell
$links = @(
    @{ Path = Join-Path ([Environment]::GetFolderPath('Startup')) 'Nexus Core V2 Connection.lnk'; Args = '-m nexus.workspace_service run' },
    @{ Path = Join-Path ([Environment]::GetFolderPath('Programs')) 'NEXUS Connection.lnk'; Args = '-m nexus.workspace_service panel' }
)
# Reject an unrelated collision before modifying either shortcut.
foreach ($item in $links) {
    if (Test-Path -LiteralPath $item.Path) {
        $link = $shell.CreateShortcut($item.Path)
        if ($link.TargetPath -ne $python -or $link.Arguments -ne $item.Args) {
            throw "Unrecognized shortcut: $($item.Path)"
        }
    }
}
foreach ($item in $links) {
    if ($Action -eq 'Remove') {
        if (Test-Path -LiteralPath $item.Path) { Remove-Item -LiteralPath $item.Path }
    } else {
        $link = $shell.CreateShortcut($item.Path)
        $link.TargetPath = $python
        $link.Arguments = $item.Args
        $link.WorkingDirectory = $repo
        $link.Description = 'Nexus Core V2 connection for the current Windows user'
        $link.Save()
        $check = $shell.CreateShortcut($item.Path)
        if ($check.TargetPath -ne $python -or $check.Arguments -ne $item.Args -or $check.WorkingDirectory -ne $repo) {
            throw 'Shortcut verification failed'
        }
    }
    [pscustomobject]@{ Action = $Action; Path = $item.Path }
}
