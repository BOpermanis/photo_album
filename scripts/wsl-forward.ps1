# Run this in an elevated (Administrator) PowerShell on Windows.
# Forwards Windows port 8000 to the WSL2 server and opens the firewall,
# so your phone can reach the app at http://<this-PC-LAN-IP>:8000.
#
# NOTE: the WSL IP changes when WSL restarts/reboots. Re-run this script
# after a reboot (it deletes the old rule and re-adds with the current IP).
#
# Usage (from Windows, Admin PowerShell):
#   powershell -ExecutionPolicy Bypass -File \\wsl$\<distro>\home\bruno\repos\photo_album\scripts\wsl-forward.ps1

$port = 8000

# Get the current WSL2 IP (first address from `hostname -I`).
$wslIp = (wsl hostname -I).Trim().Split(" ")[0]
if (-not $wslIp) {
    Write-Host "Could not determine WSL IP. Is WSL running?" -ForegroundColor Red
    exit 1
}

# Forward Windows :8000 -> WSL :8000 (bind to the real WSL IP, NOT 127.0.0.1,
# which would loop back into this proxy and cause ERR_EMPTY_RESPONSE).
netsh interface portproxy delete v4tov4 listenport=$port listenaddress=0.0.0.0 2>$null | Out-Null
netsh interface portproxy add v4tov4 listenport=$port listenaddress=0.0.0.0 connectport=$port connectaddress=$wslIp

# Allow inbound traffic on the port.
if (-not (Get-NetFirewallRule -DisplayName "PhotoAlbum $port" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName "PhotoAlbum $port" -Direction Inbound -LocalPort $port -Protocol TCP -Action Allow | Out-Null
}

$ip = (Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.InterfaceAlias -notmatch 'WSL|Loopback|Default Switch|Tailscale' -and
                   $_.IPAddress -notlike '169.*' -and $_.IPAddress -notlike '127.*' } |
    Select-Object -First 1).IPAddress

Write-Host ""
Write-Host "Forwarding Windows :$port -> WSL ${wslIp}:$port, firewall opened." -ForegroundColor Green
Write-Host "Active portproxy rules:" -ForegroundColor DarkGray
netsh interface portproxy show v4tov4
Write-Host ""
Write-Host "Start the server in WSL (python run.py), then open on your phone:" -ForegroundColor Green
Write-Host "  http://${ip}:$port" -ForegroundColor Cyan
Write-Host ""
Write-Host "To undo later:" -ForegroundColor DarkGray
Write-Host "  netsh interface portproxy delete v4tov4 listenport=$port listenaddress=0.0.0.0"
Write-Host "  Remove-NetFirewallRule -DisplayName 'PhotoAlbum $port'"
