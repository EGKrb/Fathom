# x64dbg EGKrb 2026.10.28

Fork **EGKrb/x64dbg** — intégration de **HvKdFusion** : débogage noyau et forensics mémoire des VM Hyper-V, pilotés depuis x64dbg.

## Nouveautés

### HvKdFusion depuis la barre de commande

x64dbg débogue des processus en mode utilisateur. HvKdFusion couvre le **noyau et la mémoire entière d'une machine** — une VM Hyper-V live ou une image mémoire (crash dump noyau, dump brut) — en reliant LiveCloudKd, WinDbg/kd, MemProcFS + LeechCore (+ Dokany) et ntoseye sous une seule CLI. Le plugin **ExtraCmds** (v2) ajoute les commandes :

- **`hvkd`** — menu interactif HvKdFusion dans une console (choix de la VM, puis action) ;
- **`hvkd action [options]`** — n'importe quelle action, par ex. `hvkd dump --vm 0 --out C:\dumps\vm0.raw`, `hvkd debug --vm 0 --engine windbg`. L'élévation (UAC) est demandée uniquement quand l'action en a besoin ;
- **`hvkd.log action [options]`** — exécute une action sans fenêtre et écrit la sortie dans le journal d'x64dbg (`hvkd.log list`, `hvkd.log read --vm 0 --addr 0x1000 --size 0x100`) ;
- **`hvkd.check [--deep]`** — tableau de préparation de chaque composant, dans le journal ;
- **`wineye fichier`** / **`wineye --live --vm 0`** — REPL façon WinDbg (`ps`, `lm`, `!process`, `x nt!…`, `db/dq`, `dt`, `!vad`, `!handle`, `s`) sur un dump noyau, une image brute ou une VM live, sans WSL ni Dokany ;
- **`.init`** reconnaît maintenant les crash dumps **noyau** (`MEMORY.DMP`, signatures `PAGEDUMP`/`PAGEDU64`) et les images `.raw`/`.mem`/`.vmem` et les ouvre dans wineye ; les minidumps de processus s'ouvrent toujours dans la visionneuse.

Les outils Python sont livrés dans `release\hvkdfusion\` (dossier `hvkdfusion\` du dépôt). Les composants tiers (LiveCloudKd, MemProcFS, LeechCore, Dokany, ntoseye, WinDbg) ne sont **pas inclus** : lancez une fois `python hvkdfusion.py setup`. Python 3.11+ requis sur le `PATH`. Voir `hvkdfusion\README.md`.

HvKdFusion est aussi publié comme projet autonome : <https://github.com/EGKrb/HvKdFusion>.

## Corrections de build

- `tools\build-minidump-viewer.ps1` : fixe `CMAKE_SYSTEM_VERSION` sur la version réelle de Windows, sinon `cpp-httplib` refuse de configurer la visionneuse (il lit `6.2.9200`, la version renvoyée à un processus sans manifeste, et croit tourner sous Windows 8).

## Contenu de l'archive

Identique aux releases précédentes (`release\` avec `x96dbg.exe`, `x64\`, `x32\`, visionneuse `minidump.exe`, plugins ScyllaHide / xAnalyzer / PE-sieve / pybridge / ExtraCmds, client pybridge), plus `release\hvkdfusion\`. `x64dbg-egkrb_<version>_symbols.zip` contient les `.pdb`.

## Checksums

Voir `SHA256SUMS.txt`.
