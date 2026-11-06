# Start-Process joins argument arrays without quoting, so preserve Windows argv boundaries explicitly.
function ConvertTo-NativeArgumentString($arguments) {
    return (($arguments | ForEach-Object {
        $escaped = [regex]::Replace([string]$_, '(\\*)"', '$1$1\"')
        $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
        '"' + $escaped + '"'
    }) -join ' ')
}
