# opus.ps1 — Windows PowerShell entry point for Klipper Opus 4.7 interactive session
#
# Amac: Windows'tan tek komutla klipper'da Opus interactive Claude Code oturumu ac.
# Bu oturumda Opus (kontrolcu) ile konusursun; ayri Windows terminal'inde Sonnet
# (uretici) calistirip iki ajan memory/notes uzerinden haberlesip ortak is bitir.
#
# Kurulum (Windows):
#   1) Bu dosyayi F:\projelerim\scripts\opus.ps1 olarak kaydet (veya istedigin yere)
#   2) PowerShell profilinde (Microsoft.PowerShell_profile.ps1) dot-source et:
#        . F:\projelerim\scripts\opus.ps1
#      VEYA mevcut klipper-isbirligi.ps1'in icine kopyala
#   3) SSH config'inde 'klipper' alias'i tanimli olmali (Tailscale ile passwordless)
#        Test:  ssh klipper "echo ok"
#
# Kullanim:
#   opus                            # /opt/linux-ai-server'da Opus aç
#   opus -Project bilge-arena-en   # /home/klipperos/work/bilge-arena-en'de aç
#   opus -Cwd /opt/foo             # ozel yolda aç
#   opus -Sonnet                    # Opus yerine Sonnet aç (Max plan, ucretsiz interactive)
#
# Onemli:
#   - Headless mode (claude -p) YASAK — API charge eder, Max plan kapsami disi
#   - Interactive oturum Max plan'a sayar (ucretsiz Pro/Enterprise icin)
#   - Iki ajan haberlesirken bu makinelerde memory API key'i ortak: KLIPPER_MEMORY_KEY

function opus {
    [CmdletBinding()]
    param(
        [Parameter(Position = 0)]
        [string]$Project = "",
        [string]$Cwd = "",
        [switch]$Sonnet
    )

    # Calistirma dizini cozumlemesi
    if ($Project) {
        $targetCwd = "/home/klipperos/work/$Project"
    } elseif ($Cwd) {
        $targetCwd = $Cwd
    } else {
        $targetCwd = "/opt/linux-ai-server"
    }

    # Model secimi
    $model = if ($Sonnet) { "sonnet" } else { "opus" }
    $label = if ($Sonnet) { "Sonnet" } else { "Opus" }

    Write-Host ""
    Write-Host "  Klipper $label session" -ForegroundColor Cyan
    Write-Host "  cwd: $targetCwd" -ForegroundColor DarkGray
    Write-Host "  model: $model" -ForegroundColor DarkGray
    if ($Sonnet) {
        Write-Host ""
        Write-Host "  Sonnet hatirlatma: oturuma su komutla basla:" -ForegroundColor DarkYellow
        Write-Host "    Read /opt/linux-ai-server/scripts/prompt-sonnet-uretici.md" -ForegroundColor Yellow
        Write-Host "    ve bu prompt'taki kurallari oturum boyunca uygula." -ForegroundColor DarkYellow
    }
    Write-Host ""

    # -t flag: TTY allocate (interactive Claude Code icin sart)
    # PowerShell'in tirnak escape'i icin tek tirnak icinde cd komutu
    ssh -t klipper "cd '$targetCwd' && claude --model $model"

    $exitCode = $LASTEXITCODE
    Write-Host ""
    if ($exitCode -eq 0) {
        Write-Host "  $label session ended cleanly" -ForegroundColor Green
    } else {
        Write-Host "  $label session exit=$exitCode" -ForegroundColor Yellow
    }
}

# Bonus: ortak is icin hizli inbox kontrol (Windows'tan)
function opus-inbox {
    <#
    .SYNOPSIS
    Klipper memory'sinde Sonnet'ten Opus'a (klipper) okunmamis not var mi bak.
    Iki ajan haberlesmesinde useful: 'Opus, sonuc geldi mi?' check.
    #>
    if (-not $env:KLIPPER_MEMORY_KEY) {
        Write-Host "KLIPPER_MEMORY_KEY env var tanimli degil. User env var'a ekle." -ForegroundColor Red
        return
    }
    $headers = @{ "X-Memory-Key" = $env:KLIPPER_MEMORY_KEY }
    $uri = "http://100.113.153.62:8420/api/v1/memory/notes?device=klipper&unread_only=true"
    try {
        $res = Invoke-RestMethod -Uri $uri -Headers $headers -Method Get
        if ($res.Count -eq 0) {
            Write-Host "  inbox bos (okunmamis not yok)" -ForegroundColor DarkGray
        } else {
            Write-Host "  $($res.Count) okunmamis not:" -ForegroundColor Cyan
            $res | ForEach-Object {
                Write-Host ("    #{0,-4} {1,-12} {2}" -f $_.id, $_.from_device, $_.title)
            }
        }
    } catch {
        Write-Host "  inbox query failed: $_" -ForegroundColor Red
    }
}
