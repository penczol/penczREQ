$ErrorActionPreference = "Stop"

function New-ContainerSecret {
    param([int]$Bytes = 48)
    $buffer = New-Object byte[] $Bytes
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($buffer)
    return [Convert]::ToBase64String($buffer).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

Write-Output "SESSION_SECRET=$(New-ContainerSecret)"
Write-Output "CONTROL_SESSION_SECRET=$(New-ContainerSecret)"
Write-Output "CONFIG_ENCRYPTION_KEY=$(New-ContainerSecret)"
