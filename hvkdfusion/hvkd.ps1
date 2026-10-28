<#
    hvkd.ps1 - lanceur HvKdFusion avec élévation UAC automatique.

    Exécute hvkdfusion.py en administrateur (requis pour toute opération
    mémoire Hyper-V). Si la session n'est pas déjà élevée, relance une
    fenêtre PowerShell élevée (prompt UAC) et y exécute la commande.

    Exemples :
        .\hvkd.ps1                 # menu interactif, élevé
        .\hvkd.ps1 list
        .\hvkd.ps1 debug --vm 0 --engine windbg
        .\hvkd.ps1 dump --vm 0 --type linear --out guest.raw

    Sans besoin d'admin (exécutées directement, sans UAC) : setup, config,
    check, et l'analyse d'un fichier existant (wineye --dump, ntoseye --dump).

    HVKD_KEEP_OPEN=1 (posé par la commande x64dbg 'hvkd') garde la fenêtre
    ouverte à la fin pour lire la sortie.
#>

# Pas de param() : tous les arguments passent tels quels à hvkdfusion.py, y
# compris ceux qui ressemblent à des paramètres PowerShell (wineye -c "ps;lm").
$Rest = @($args)

$ErrorActionPreference = 'Stop'

$here   = Split-Path -Parent $MyInvocation.MyCommand.Definition
$script = Join-Path $here 'hvkdfusion.py'

if (-not (Test-Path $script)) {
    Write-Error "Introuvable : $script"
    exit 1
}

# Résoudre l'interpréteur Python (python.exe puis py.exe).
$py = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (Get-Command py.exe -ErrorAction SilentlyContinue).Source }
if (-not $py) {
    Write-Error 'Python introuvable sur le PATH (python.exe ou py.exe).'
    exit 1
}

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($id)
    return $principal.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
}

# Sans argument -> menu interactif.
if (-not $Rest -or $Rest.Count -eq 0) {
    $Rest = @('menu')
}

# Aide/raccourcis.
if ($Rest[0] -in @('help', '-h', '--help', '/?')) {
    @'
hvkd.ps1 - lanceur HvKdFusion (élévation UAC auto)

  .\hvkd.ps1                      menu interactif (élevé)
  .\hvkd.ps1 list                 énumère les VM Hyper-V
  .\hvkd.ps1 debug --vm 0 --engine windbg
  .\hvkd.ps1 dump  --vm 0 --type linear --out guest.raw
  .\hvkd.ps1 read  --vm 0 --addr 0x10000 --size 0x1000

  Raccourcis ntoseye (analyse de dump) :
  .\hvkd.ps1 ntoseye --vm 0                 crash dump de la VM puis REPL ntoseye
  .\hvkd.ps1 ntoseye --dump C:\x\MEMORY.DMP analyse un .dmp existant (sans admin)
  .\hvkd.ps1 ntoseye --dump C:\x\MEMORY.DMP --mode mcp --http 127.0.0.1:8080

Toute sous-commande de hvkdfusion.py passe en direct. 'setup' n'a pas besoin d'admin.
'@
    exit 0
}

# Actions sans besoin d'admin : on les exécute directement, sans prompt UAC.
# L'analyse d'un dump DÉJÀ existant (ntoseye/wineye --dump) en fait partie ; la
# génération d'un dump depuis une VM (--vm, --from-vm, --live) passe par la
# branche élevée plus bas.
$needsVm = ($Rest -contains '--vm') -or ($Rest -contains '--from-vm') -or ($Rest -contains '--live')
$isFileAnalysis = ($Rest[0] -in @('ntoseye', 'wineye')) -and ($Rest -contains '--dump') -and -not $needsVm
$noAdmin = ($Rest[0] -in @('setup', 'config', 'check')) -or $isFileAnalysis

if ((Test-Admin) -or $noAdmin) {
    # Déjà administrateur (ou action sans besoin d'admin) : exécuter directement.
    & $py $script @Rest
    $code = $LASTEXITCODE
    if ($env:HVKD_KEEP_OPEN) {
        Write-Host ''
        Read-Host 'Terminé. Entree pour fermer' | Out-Null
    }
    exit $code
}

# Pas administrateur : relancer une fenêtre élevée (prompt UAC).
# On garde la fenêtre ouverte à la fin pour lire la sortie.
$argline = ($Rest | ForEach-Object { '"' + ($_ -replace '"', '`"') + '"' }) -join ' '
$inner = @"
Set-Location "$here"
& "$py" "$script" $argline
Write-Host ''
Read-Host 'Terminé. Entree pour fermer'
"@

Write-Host '[*] élévation (UAC)...'
try {
    Start-Process -Verb RunAs -FilePath 'powershell.exe' `
        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', $inner)
}
catch {
    Write-Error "Élévation refusée ou échouée : $($_.Exception.Message)"
    exit 1
}
