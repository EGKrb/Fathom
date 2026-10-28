# x64dbg (fork EGKrb) %VERSION%

Fork non officiel de x64dbg : <https://github.com/EGKrb/x64dbg>
Commit : `%COMMIT%`

## Démarrer

- `x96dbg.exe` : lanceur (choisit 32/64 bits, peut enregistrer le menu contextuel de l'Explorateur).
- `x64\x64dbg.exe` : programmes 64 bits. `x32\x32dbg.exe` : programmes 32 bits.
- `x64\headless.exe` / `x32\headless.exe` : x64dbg sans interface, piloté par script (`-cf script.txt`) ou par Python.

Le dossier doit être dans un emplacement où vous avez le droit d'écrire (pas `C:\Program Files`) : la configuration et les bases de données sont enregistrées à côté des exécutables.

## Plugins inclus

| Plugin | Rôle |
|---|---|
| ScyllaHide | masque le débogueur aux protections anti-debug (menu Plugins > ScyllaHide > Options). Le profil actif par défaut est « VMProtect x86/x64 » ; choisissez « Basic » pour un programme non protégé si celui-ci se comporte mal |
| xAnalyzer | analyse automatique : arguments des appels d'API commentés dans le désassemblage (définitions dans `plugins\apis_def`) |
| pybridge | pilotage depuis Python (`pybridge\README.md`) |
| ExtraCmds | commandes `.init`, `.save`, `scylla_hide.enable` / `.disable` / `.status`, `hvkd`, `hvkd.log`, `hvkd.check`, `wineye` |

**OllyDumpEx** (dump de processus en PE) n'a pas de licence de redistribution : installez-le avec

```powershell
powershell -ExecutionPolicy Bypass -File .\install-plugins.ps1 -Plugins OllyDumpEx
```

## Nouveautés par rapport à x64dbg officiel

- Commande `TraceExport` : convertit une trace (`.trace64`/`.trace32`) en JSON ou CSV.
  ```
  TraceExport "C:\traces\cible.trace64", "C:\traces\cible.json"
  ```
- `pybridge\python\x64dbg_bridge.py` : client Python (breakpoints, pas à pas, registres, mémoire, traces).
- `pybridge\headless\run-headless.ps1` : modèles de scripts prêts à l'emploi (journal d'API, trace + export, dump, breakpoint conditionnel).

- Commandes ajoutées (plugin ExtraCmds) :
  ```
  .init "C:\cible.exe"            débogue le programme (comme init)
  .init "C:\dumps\cible.dmp"      ouvre le dump dans le visualiseur x64\minidump.exe
  .save                           sauvegarde commentaires, labels, breakpoints
  .save "C:\out\cible.dmp"        minidump complet du processus
  .save "C:\out\zone.bin", rsp    zone mémoire contenant l'adresse
  scylla_hide.status              profil ScyllaHide actif
  scylla_hide.enable basic        active ScyllaHide avec ce profil
  scylla_hide.disable             désactive ScyllaHide
  ```
  Un dump n'est pas un processus : il s'ouvre dans le visualiseur (mémoire, désassemblage, hexadécimal, threads, registres), pas dans le débogueur.

- HvKdFusion (dossier `hvkdfusion\`) : débogage noyau et forensics mémoire des VM Hyper-V (LiveCloudKd, WinDbg/kd, MemProcFS, ntoseye) et wineye, un REPL façon WinDbg sur les dumps noyau et images mémoire. Les composants tiers ne sont pas inclus : voir `hvkdfusion\README.md`, puis `python hvkdfusion\hvkdfusion.py setup`. Python 3.11+ requis.
  ```
  hvkd                            menu interactif (choix de la VM puis de l'action)
  hvkd.check                      état de chaque composant, dans le journal
  hvkd dump --vm 0 --out C:\dumps\vm0.raw
  .init "C:\dumps\vm0.raw"         dump noyau / image brute : ouvert dans wineye
  wineye --live --vm 0            VM live (gel, reprise, registres)
  ```

Documentation complète : <https://github.com/EGKrb/x64dbg#readme>
Licences : dossier `licenses\` (x64dbg, ScyllaHide et HvKdFusion : GPL-3.0, xAnalyzer : MIT).
