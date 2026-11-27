> 🇬🇧 **This README is also available in [English](README.md).**

# Fathom

<img width="100" src="src/fathom-logo.png" alt="Fathom"/>

[![Licence : GPL-3.0](https://img.shields.io/badge/License-GPL--3.0-blue.svg)](LICENSE) [![Plateforme : Windows](https://img.shields.io/badge/Plateforme-Windows%20x86%2Fx64-0078D6.svg)](#) [![Release](https://img.shields.io/badge/release-v1.11.0-success.svg)](https://github.com/EGKrb/Fathom/releases/latest) [![Basé sur x64dbg](https://img.shields.io/badge/bas%C3%A9%20sur-x64dbg-444.svg)](https://github.com/x64dbg/x64dbg)

**Fathom** — une boîte à outils Windows de rétro-ingénierie et de forensic mémoire, de l'instruction isolée à la machine entière. Elle est **construite sur [x64dbg](https://github.com/x64dbg/x64dbg)** (GPL-3.0), le débogueur open source pour Windows (analyse de malwares, rétro-ingénierie d'exécutables sans code source), dont elle reprend la branche `development` officielle, et ajoute :

- **`TraceExport`** : conversion des traces d'exécution en **JSON** ou **CSV** lisibles par n'importe quel outil, en traces texte **Tenet** / **Frinet** pour les explorer dans IDA Pro, ou au format **`.ftt`** indexé et compressé, avec une chronologie par thread et des instantanés mémoire ;
- un onglet **Time Travel** et les commandes `tt.*` : naviguer dans une trace **en arrière comme en avant** sans quitter le débogueur — n'importe quel thread à n'importe quel instant, registres, mémoire reconstruite à cet instant, qui a écrit à cette adresse ;
- un **émulateur** (commandes `emu.*`, [dumpulator](https://github.com/mrexodia/dumpulator)) : **appeler une fonction du processus débogué, ou faire avancer son thread, dans un émulateur** où les appels système et les exceptions de Windows sont implémentés en Python, à partir d'une copie de son état : le processus ne bouge pas, et le code émulé peut être enregistré pour l'onglet Time Travel ;
- l'**exécution symbolique** avec [Triton](https://github.com/JonathanSalwan/Triton) (`sym.*`, `taint.*`) : sur le débogué ou sur une trace enregistrée, **résoudre l'entrée qui passe une vérification** et l'écrire dans le processus, inverser un branchement, simplifier des expressions obscurcies, suivre des données teintées ;
- le **décompilateur de Ghidra** dans le débogueur (`decompile`), la **dévirtualisation de VMProtect** (`novmp`, NoVmp + VTIL, x64 et x86), le code virtualisé trouvé et simplifié dans les traces (`vm.*`, la méthode de VMHunt), **angr** / **miasm** sur l'état du débogué (`angr.*`, `miasm.*`), **remill** (`remill.lift` : le code machine en IR LLVM, compilé pour Windows), le décompilateur **rev.ng** (`revng.decompile`, porté sous Windows) et **Binary Ninja** à côté du débogueur (`binja`, plugin `fathom_binja`) ;
- **pybridge** : pilotage complet d'x64dbg **depuis Python** (breakpoints, pas à pas, registres, mémoire, traces) ;
- des **modèles de scripts** prêts à l'emploi pour **`headless.exe`** (x64dbg sans interface) ;
- un **visualiseur de dumps mémoire** (`.dmp`) et des commandes pratiques : **`.init`** (exécutable ou dump), **`.save`**, **`scylla_hide.enable` / `.disable` / `.status`** ;
- **HvKdFusion** : **débogage noyau et forensics mémoire Hyper-V** : `hvkd` (LiveCloudKd + WinDbg/kd + MemProcFS + ntoseye) et **wineye** — un REPL natif façon WinDbg sur dumps noyau, images brutes et VM live, avec les modes serveur **MCP / DAP / gdbserver** — plus un **onglet GUI « HvKdFusion »** dans le débogueur ;
- les plugins **ScyllaHide** et **xAnalyzer** inclus dans la release, **OllyDumpEx** installable en une commande.

**[⬇ Télécharger la dernière release](https://github.com/EGKrb/Fathom/releases/latest)** · [Changelog](CHANGELOG.md) · [Basé sur x64dbg](https://github.com/x64dbg/x64dbg)

### Téléchargement

Récupérez **`Fathom_<version>.zip`** depuis la [dernière release](https://github.com/EGKrb/Fathom/releases/latest), décompressez, et lancez **`release\Fathom.exe`** (le lanceur choisit 32 ou 64 bits). `Fathom_<version>_symbols.zip` contient les symboles `.pdb` ; `SHA256SUMS.txt` les empreintes. Les composants tiers d'HvKdFusion ne sont pas inclus — lancez une fois `python hvkdfusion.py setup` (voir [hvkdfusion/README.md](hvkdfusion/README.md)).

> **Plateforme : Windows (x86/x64).** x64dbg débogue des processus Windows via l'API de débogage Windows ; le débogueur, `headless.exe` et les plugins sont donc Windows uniquement. Le **client** Python de pybridge est agnostique (bibliothèque standard seule), mais il pilote un x64dbg — qui tourne sous Windows.

---

## Sommaire

1. [Installation](#installation)
2. [Différences avec x64dbg officiel](#différences-avec-x64dbg-officiel)
3. [Comment fonctionne x64dbg](#comment-fonctionne-x64dbg)
4. [TraceExport : exporter les traces](#traceexport--exporter-les-traces)
5. [Time Travel : explorer une trace à reculons](#time-travel--explorer-une-trace-à-reculons)
6. [Émulateur : exécuter le code du débogué sans l'exécuter](#émulateur--exécuter-le-code-du-débogué-sans-lexécuter)
7. [Exécution symbolique : Triton](#exécution-symbolique--triton)
8. [Décompilateur, dévirtualisation, angr, miasm](#décompilateur-dévirtualisation-angr-miasm)
9. [pybridge : piloter x64dbg depuis Python](#pybridge--piloter-x64dbg-depuis-python)
10. [Modèles pour headless.exe](#modèles-pour-headlessexe)
11. [Commandes ajoutées et dumps mémoire](#commandes-ajoutées-et-dumps-mémoire)
12. [HvKdFusion : débogage noyau et forensics mémoire Hyper-V](#hvkdfusion--débogage-noyau-et-forensics-mémoire-hyper-v)
13. [Plugins inclus](#plugins-inclus)
14. [Compiler soi-même](#compiler-soi-même)
15. [Organisation du dépôt](#organisation-du-dépôt)
16. [Licences et crédits](#licences-et-crédits)

---

## Installation

Depuis la [page des releases](https://github.com/EGKrb/Fathom/releases/latest), au choix :

- **Installateur** — lancez `Fathom_<version>_setup.exe`. Il installe **par utilisateur, sans admin** dans `%LOCALAPPDATA%\Programs\Fathom` — un dossier inscriptible, ce qui compte car Fathom (x64dbg) garde sa configuration (`x64dbg.ini`) et ses bases (`db\`) à côté des exécutables. Il ajoute des raccourcis (menu Démarrer + bureau) et un désinstalleur dans Ajout/Suppression de programmes.
- **Portable** — téléchargez `Fathom_<version>.zip` et extrayez-le dans un dossier **où vous avez le droit d'écrire** (ex. `C:\Outils\Fathom`, **pas** `C:\Program Files`), pour la même raison.

Puis lancez **Fathom** (le raccourci, ou `Fathom.exe` — le lanceur choisit l'architecture), ou directement `x64\x64dbg.exe` / `x32\x32dbg.exe` pour une cible 64 ou 32 bits.

Facultatif : installez OllyDumpEx (non redistribuable, voir [Plugins inclus](#plugins-inclus)) :
   ```powershell
   cd release
   powershell -ExecutionPolicy Bypass -File .\install-plugins.ps1 -Plugins OllyDumpEx
   ```

Contenu de l'archive :

```
release\
  Fathom.exe                 lanceur
  x64\  x32\                 débogueur 64 / 32 bits, headless.exe, plugins\ (Triton.dp64 avec libz3.dll...)
  x64\minidump.exe           visualiseur de dumps mémoire (.dmp)
  translations\              interface traduite (dont le français : Options > Langue)
  themes\                    thème sombre
  pybridge\                  client Python, exemples, modèles headless
  emulator\                  émulateur des commandes emu.* (dumpulator, Python)
  novmp\                     NoVmp.exe / NoVmp32.exe et vtildump (commande novmp : dévirtualisation de VMProtect, x64 / x86)
  remill\                    remill-lift-17.exe et ses sémantiques (commande remill.lift : IR LLVM)
  binja\                     fathom_binja, le plugin Binary Ninja (synchronisation avec Fathom)
  revng\                     rev.ng pour Windows (commande revng.decompile), dans son archive : Fathom_<version>_revng.zip
  analysis\                  scripts angr et miasm (commandes angr.*, miasm.*), miasm
  install-plugins.ps1        installe/met à jour ScyllaHide, xAnalyzer, OllyDumpEx
  licenses\                  licences d'x64dbg et des plugins
  LISEZMOI.md                démarrage rapide
pluginsdk\                   en-têtes et bibliothèques pour écrire des plugins
commithash.txt               commit exact de la compilation
```

Un second fichier, `Fathom_<version>_symbols.zip`, contient les symboles de débogage (`.pdb`), utiles seulement pour analyser un plantage d'x64dbg lui-même. `SHA256SUMS.txt` donne les empreintes des deux archives.

---

## Différences avec x64dbg officiel

| | x64dbg officiel | Ce fork |
|---|---|---|
| Code de base | releases publiées (ex. 2026.05.27) | branche `development` récente (fonctionnalités et correctifs pas encore publiés officiellement) |
| Export des traces | binaire `.trace64` lisible uniquement par x64dbg ; export CSV possible seulement dans l'interface | commande `TraceExport` → JSON, CSV, Tenet, Frinet ou `.ftt`, registres étendus, chronologies par thread, aussi en script et dans `headless.exe` |
| Time travel | vue des traces (liste des instructions, vers l'avant) | onglet Time Travel et commandes `tt.*` : n'importe quel thread à n'importe quel instant, mémoire reconstruite, exécution / écriture précédente |
| Émulation | — | commandes `emu.*` : une fonction du débogué appelée, ou son thread poursuivi, dans un émulateur qui implémente les appels système de Windows (dumpulator), à partir d'un minidump de son état ; traces émulées dans Time Travel |
| Exécution symbolique | — | plugin Triton (`sym.*`, `taint.*`) : exécution symbolique et analyse de teinte sur l'état du débogué ou une trace enregistrée, résolution Z3 écrite dans le débogué, synthèse d'expressions |
| Décompilation, désobscurcissement | — | décompilateur de Ghidra sur la mémoire du processus (`decompile`), dévirtualisation de VMProtect (`novmp`, x64 et x86), machines virtuelles dans les traces (`vm.*`), angr et miasm sur le débogué (`angr.*`, `miasm.*`), IR LLVM avec remill (`remill.lift`), décompilateur de rev.ng (`revng.decompile`), Binary Ninja (`binja`, plugin) |
| Automatisation | langage de script x64dbg, SDK C/C++ | + client Python (pybridge) + modèles headless avec lanceur |
| Dumps mémoire (`.dmp`) | création seulement (`minidump`) | + visualiseur de dumps (`minidump.exe`), ouvert par `.init fichier.dmp` ; les dumps noyau et images mémoire brutes s'ouvrent dans wineye |
| Noyau / machine entière | — (mode utilisateur seulement) | HvKdFusion : lecture/écriture/dump de la mémoire d'une VM Hyper-V, WinDbg/kd sur une VM, montage MemProcFS, REPL wineye (`hvkd`, `wineye`) |
| Commandes | — | `.init`, `.save`, `scylla_hide.enable` / `.disable` / `.status`, `hvkd`, `hvkd.log`, `hvkd.check`, `wineye`, `tt.*`, `emu.*`, `novmp`, `angr.*`, `miasm.*`, `remill.lift`, `revng.decompile`, `binja` (plugin ExtraCmds), `sym.*`, `taint.*`, `vm.*` (plugin Triton), `decompile` (plugin Decompiler) |
| Plugins | aucun | ScyllaHide et xAnalyzer inclus, pybridge, ExtraCmds, Triton et Decompiler inclus, OllyDumpEx via `install-plugins.ps1` |
| Compilation | — | correctif d'un `using namespace std` qui cassait le *unity build* (`patternfind.cpp`) |

Le cœur du débogueur n'est pas modifié : tout ce qui fonctionne dans x64dbg fonctionne ici à l'identique.

> Basé sur une version de développement : elle passe la suite de tests automatiques du projet (81 tests en 64 bits), mais peut être moins éprouvée qu'une release officielle.

---

## Comment fonctionne x64dbg

x64dbg est découpé en modules qui communiquent par une couche commune, le *bridge* :

```
 x64dbg.exe / x32dbg.exe        headless.exe
   (interface Qt, x64gui.dll)     (sans interface : console + scripts)
            \                      /
             x64bridge.dll  ── API commune (DbgCmdExec, DbgMemRead, Gui*...)
                    |
              x64dbg.dll  ── le débogueur : commandes, expressions, scripts,
                    |        breakpoints, traces, base de données, plugins
                    |
        TitanEngine / GleeBug ── moteur bas niveau (API de débogage Windows)
                    |
             processus débogué
```

- **Le débogueur (`x64dbg.dll`)** fait tout le travail : il lance ou s'attache au processus, gère les breakpoints (logiciels `INT3`, matériels via les registres de debug, mémoire via les protections de page), évalue les expressions et exécute les commandes.
- **L'interface** et **`headless.exe`** ne sont que des clients du débogueur. `headless.exe` lit les commandes sur son entrée standard, exécute un script avec `-cf`, et écrit le journal sur sa sortie : c'est ce qui permet l'automatisation.
- **Tout est une commande** : chaque action de l'interface correspond à une commande texte (`bp`, `run`, `StepInto`, `savedata`...) utilisable dans la barre de commande, dans un script ou par un plugin.
- **Les expressions** (`rax+8`, `[rsp]`, `kernel32.CreateFileW`, `mod.base(cible.exe)`, `arg.get(0)`) servent partout où une valeur est attendue. **Les nombres y sont en hexadécimal par défaut** ; `.100` signifie 100 en décimal.
- **Les plugins** (`.dp64`/`.dp32` dans `plugins\`) sont chargés par le débogueur, donc aussi dans `headless.exe`. Ils peuvent ajouter des commandes et appeler toute l'API.

### Les traces

Une **trace** enregistre chaque instruction exécutée pendant un pas à pas automatique :

```
StartTraceRecording "C:\traces\cible.trace64"   // ouvre le fichier d'enregistrement
TraceIntoConditional 0, .10000                  // trace jusqu'à ce que la condition soit vraie (0 = jamais), max 10 000 instructions
StopTraceRecording
```

Pour chaque instruction, x64dbg écrit dans le fichier `.trace64` : le thread, les octets de l'instruction, les **registres modifiés** depuis l'instruction précédente (avec un état complet tous les 512 pas) et les **accès mémoire** (adresse, ancienne et nouvelle valeur). Ce format compact (décrit dans [`docs/developers/tracefile.md`](docs/developers/tracefile.md)) n'est lisible que par x64dbg : c'est ce que `TraceExport` résout.

---

## TraceExport : exporter les traces

```
TraceExport fichier_trace, fichier_sortie[, format[, slide]][, regs=...][, thread=...][, snapshot=...]
```

| Argument | Description |
|---|---|
| `fichier_trace` | trace à lire : `.trace64` avec x64dbg, `.trace32` avec x32dbg, ou un `.ftt` |
| `fichier_sortie` | fichier créé (écrasé s'il existe) |
| `format` | `json`, `csv`, `tenet`, `frinet` ou `ftt`. Sans ce 3e argument : `csv` si le nom se termine par `.csv`, `frinet` s'il se termine par `.tenet`, `ftt` s'il se termine par `.ftt`, sinon `json` |
| `slide` | `frinet` uniquement, facultatif : décalage ASLR (base du module tracé à l'exécution, par ex. `mod.main()`). Par défaut : base du module débogué si la trace lui appartient, sinon aucun |
| `regs=` | registres écrits par `json`, `csv` et `ftt` (niveau de détail) : `gpr` (défaut), `fpu` (x87), `sse` (xmm, mxcsr), `avx` (+ moitiés hautes des ymm), `all`, ou `fpu+sse`… |
| `thread=` | n'exporter que la chronologie de ce thread (par ex. `thread=tid()`) |
| `snapshot=` | `ftt` : instructions entre deux instantanés mémoire (`.65536` par défaut) |

Après la commande, **`$result`** contient le nombre d'instructions exportées. La commande **n'a pas besoin d'un processus en cours de débogage** : elle fonctionne aussi pour convertir des traces anciennes, et dans `headless.exe`.

### Format JSON

```json
{
"version": 1,
"arch": "x64",
"path": "C:\\cible.exe",
"hash": "0x5DD5453A5460CC36",
"instructions": [
{"index": 1, "thread": 21712, "address": "0x7FF757A81290", "module": "cible.exe", "bytes": "E8 4B 02 00 00",
 "disasm": "call 0x00007FF757A814E0",
 "regs": {"rax": "0x7FF757A8128C", "rcx": "0x6254684000", "...": "...", "rip": "0x7FF757A81290", "rflags": "0x202"},
 "mem": [{"address": "0x625451F978", "old": "0x0", "new": "0x7FF757A81295"}]}
]
}
```

| Champ | Contenu |
|---|---|
| `index` | numéro de l'instruction dans la trace (0, 1, 2...) |
| `thread` | identifiant du thread (décimal) |
| `address` | adresse de l'instruction |
| `module` | module contenant l'adresse ; présent seulement si la trace appartient au processus en cours de débogage |
| `bytes`, `disasm` | octets et désassemblage (Zydis) |
| `regs` | registres généraux, pointeur d'instruction et drapeaux **avant** l'exécution de l'instruction (`rax`…`r15`, `rip`, `rflags` en 64 bits ; `eax`…`edi`, `eip`, `eflags` en 32 bits) |
| `mem` | accès mémoire de l'instruction : adresse, valeur avant, valeur après (par blocs de la taille d'un pointeur) |

Les adresses et valeurs sont des **chaînes hexadécimales** : un entier 64 bits ne tient pas dans un nombre JSON standard. En Python : `int(valeur, 16)`. Une instruction est écrite par ligne, ce qui permet aussi `grep` sur le fichier.

### Format CSV

Une ligne par instruction, colonnes `index,thread,address,module,bytes,disasm,<registres>,memory`. La colonne `memory` liste les accès séparés par `;` : `adresse:valeur` si la mémoire n'a pas changé (lecture), `adresse:avant->après` si elle a été modifiée. Le fichier s'ouvre directement dans Excel ou avec `pandas.read_csv`.

### Formats Tenet et Frinet (explorateurs de traces pour IDA Pro)

[Tenet](https://github.com/gaasedelen/tenet) (ret2) et [Frinet](https://github.com/synacktiv/frinet) (Synacktiv) sont des plugins IDA Pro qui permettent de **remonter et avancer dans le temps** d'une trace d'exécution, de poser des points d'arrêt mémoire sur toute la trace et de retrouver l'instruction qui a écrit un registre. Les deux lisent la même trace texte lisible : une ligne par instruction, des entrées `nom=0xvaleur` séparées par des virgules — les registres modifiés depuis la ligne précédente (tous sur la première ligne), le pointeur d'instruction, et les accès mémoire `mr=0xadresse:octets` (lecture / inchangé) ou `mw=0xadresse:octets` (modifié), `octets` étant le contenu après exécution dans l'ordre de la mémoire. Les drapeaux ne sont pas exportés (aucun des deux outils ne les lit).

```
TraceExport C:\traces\cible.trace64, C:\traces\cible.log, tenet
TraceExport C:\traces\cible.trace64, C:\traces\cible.tenet, frinet, mod.main()
```

| | `tenet` | `frinet` |
|---|---|---|
| Une ligne contient | l'état **avant** son instruction, avec la mémoire accédée par la précédente (traceurs de référence de Tenet) | l'instruction, avec les registres **après** son exécution et la mémoire qu'elle a accédée (traceur Frinet) |
| Ordre | registres modifiés, puis `rip`/`eip` en dernier | `rip` d'abord, puis registres modifiés, puis mémoire |
| En-tête | — | `slide=0x…` (décalage ASLR, sert à recaler la trace sur la base IDA) |
| Architectures | x86 et x64 | x64 uniquement (le plugin Frinet ne lit que `rip`/`pc`) |
| Ouverture | IDA : *File > Load file > Tenet trace file* | `frinet index --format tenet cible.tenet cible.db`, puis IDA : *File > Load file > Open Frinet DB* |

```
rax=0x7ff7fa51128c,rcx=0xcee8e5b000,…,r15=0x0,rip=0x7ff7fa51128c          ← tenet
rsp=0xcee90fff18,rip=0x7ff7fa5114e0,mr=0xcee90fff20:0000000000000000,mw=0xcee90fff18:951251faf77f0000

slide=0x7ff7fa510000                                                      ← frinet
rip=0x7ff7fa51128c,rax=0x7ff7fa51128c,…,rsp=0xcee90fff20,…
rip=0x7ff7fa511290,rsp=0xcee90fff18,mr=0xcee90fff20:0000000000000000,mw=0xcee90fff18:951251faf77f0000
```

Limites : x64dbg enregistre chaque accès mémoire comme une valeur de la taille d'un pointeur, un accès peut donc couvrir plus d'octets que l'opérande ; une trace multi-thread est écrite comme une seule chronologie dans les formats texte, sauf si `thread=` en choisit une (le format `.ftt` garde une chronologie par thread) ; en `tenet`, la mémoire de la dernière instruction est perdue (pas de ligne suivante), et en `frinet` les registres après la dernière instruction (ou avant un changement de thread) sont inconnus.

### Fonctionnement interne

[`src/dbg/TraceExport.cpp`](src/dbg/TraceExport.cpp) lit le fichier en continu (mémoire constante, quelle que soit la taille de la trace) :

1. vérifie l'en-tête `TRAC` et son JSON (version 1, architecture identique à celle du débogueur), ou ouvre un `.ftt` par son index ;
2. pour chaque bloc d'instruction, applique les registres modifiés à l'état courant (les registres sont encodés par différence), lit les accès mémoire, puis désassemble les octets ;
3. lit les marqueurs de changement de contexte (bloc utilisateur 0x81) et ignore les autres blocs utilisateur (type ≥ 0x80) prévus par le format ;
4. écrit le JSON, le CSV, la trace Tenet, Frinet ou le `.ftt` au fil de l'eau (Tenet/Frinet gardent l'instruction précédente pour placer les accès mémoire et les registres après exécution sur la bonne ligne ; `.ftt` garde un bloc par thread et écrit les instantanés mémoire et l'index).

Sécurités : un fichier de sortie identique au fichier d'entrée est refusé (il serait écrasé) ; une trace **en cours d'enregistrement** peut être exportée, son dernier bloc incomplet est ignoré avec un avertissement ; un fichier corrompu donne un message précis indiquant après combien d'instructions.

Limites : Tenet et Frinet ne reçoivent que les registres généraux (les autres registres passent par `json`, `csv` ou `ftt` avec `regs=`) ; une trace 32 bits doit être exportée avec x32dbg et inversement.

Documentation de la commande : [`docs/commands/tracing/TraceExport.md`](docs/commands/tracing/TraceExport.md). Test automatique : [`src/tests/trace_export/`](src/tests/trace_export).

---

## Time Travel : explorer une trace à reculons

Une trace convertie au format **`.ftt`** (Fathom Time Travel, [`docs/developers/ftt.md`](docs/developers/ftt.md)) s'explore en arrière comme en avant **sans quitter le débogueur**, comme une session de débogage « time travel » :

```
StartTraceRecording "C:\traces\cible.trace64"
TraceIntoConditional 0, .100000
StopTraceRecording
TraceExport "C:\traces\cible.trace64", "C:\traces\cible.ftt", ftt, regs=all
tt.open "C:\traces\cible.ftt"          // ou l'ouvrir depuis l'onglet Time Travel
```

Ce que le format enregistre :

- **une chronologie par thread**, synchronisées par un numéro de séquence global : l'état de chaque thread est connu à tout instant, et l'on peut sauter d'un thread à l'autre à un instant T ;
- des **marqueurs de changement de contexte** explicites (horodatés quand l'enregistreur les a écrits) ;
- des **deltas stricts** : une image complète des registres par bloc, puis seulement les registres modifiés, et seulement le contenu mémoire pas encore connu ;
- des **instantanés mémoire** à intervalles réguliers (`snapshot=`) : la mémoire à n'importe quel instant se reconstruit depuis le dernier instantané plus un rejeu borné ;
- des **blocs compressés LZ4 et un index** en fin de fichier, pour accéder directement à n'importe quelle instruction ;
- les **registres au niveau de détail choisi** (`regs=gpr`, `fpu`, `sse`, `avx`, `all`).

### Onglet Time Travel (GUI)

Le plugin ExtraCmds 64 bits compilé avec Qt ajoute un onglet **Time Travel** :

- **Ouvrir une trace…** : un `.ftt`, ou une `.trace64` / `.trace32` enregistrée par x64dbg (convertie à la volée avec tous les registres) ;
- **chronologies par thread** : une piste par thread, la ligne rouge est la position ; un clic sur une piste montre **ce thread à cet instant** ;
- navigation : début / fin, instruction précédente / suivante **de tous les threads** (◀◀ ▶▶) ou **du thread affiché** (◀ ▶), aller à l'instruction n ;
- les **instructions autour de la position** (changements de contexte surlignés, au besoin le thread affiché seul), double-clic pour s'y rendre ;
- les **registres** du thread affiché, en rouge ceux modifiés par son instruction précédente, généraux / x87 / SSE / tous ;
- la **mémoire à cet instant** (adresse ou registre, `csp` par défaut) : `??` inconnu, minuscules = valeur lue plus tard (déduite) ;
- **exécution précédente / suivante** d'une adresse, **écriture précédente / suivante** à une adresse (« qui a écrit ça ? ») ;
- **Afficher dans le CPU** quand le processus débogué est celui de la trace.

Rien ne touche au processus débogué : l'onglet ne fait que lire la trace.

### Commandes

La même navigation dans les scripts et `headless.exe` (ExtraCmds, aussi en 32 bits et sans Qt) ; l'onglet suit les commandes. `$result` reçoit la position (ou la valeur lue).

| Commande | Effet |
|---|---|
| `tt.open fichier` / `tt.close` / `tt.info` | ouvre un `.ftt` (ou convertit une `.trace64`), le ferme, résumé, threads et ordre des threads |
| `tt.seek n` | aller à l'instruction n (numéro de séquence global) |
| `tt.next [n]` / `tt.prev [n]` | n instructions en avant / en arrière, tous threads |
| `tt.tnext [n]` / `tt.tprev [n]` | n instructions en avant / en arrière dans le thread affiché |
| `tt.thread tid` | voir un autre thread au même instant |
| `tt.regs [all]` | registres du thread affiché (`*` = modifié) ; `$result` = pointeur d'instruction |
| `tt.mem adresse[, taille]` | mémoire à cet instant ; `$result` = première valeur de la taille d'un pointeur |
| `tt.find adresse` / `tt.findback adresse` | exécution suivante / précédente d'une adresse |
| `tt.write adresse[, taille]` / `tt.writenext ...` | écriture précédente / suivante en mémoire |
| `tt.show` | afficher l'onglet Time Travel au premier plan |

### Enregistrement

Les traces viennent de l'enregistreur de x64dbg (pas-à-pas, `StartTraceRecording`) : le débogueur arrête le programme à chaque instruction. Il est environ **2,5 fois plus rapide que dans Fathom 1.2.0** (environ 11 800 instructions enregistrées par seconde au lieu de 4 700 avec `TraceIntoConditional` sur la machine de référence ; 19 000 au lieu de 8 900 sans enregistrement). Pendant le traitement d'un événement de débogage, tous les threads du programme sont gelés : TitanEngine lit désormais le contexte d'un thread une fois par événement au lieu d'une fois par registre, réutilise les handles de threads qu'il possède et ne suspend plus des threads déjà gelés, et l'enregistreur écrit ses blocs par paquets au lieu d'une écriture par instruction. `Engine` / `ContextCache` à 0 rétablit l'ancien comportement. Cela reste du pas-à-pas : adapté aux fonctions, fragments de code et challenges plutôt qu'à des programmes entiers.

Deux options gardent le pas-à-pas sur le code étudié :

- **`TraceSetSkip system`** (ou `TraceSetSkip only, monmodule.dll`) exécute à pleine vitesse les appels vers les modules non suivis : l'appel est franchi en step over et enregistré comme une seule instruction, marquée dans la trace et dans l'export JSON (`skip`). Sur une boucle qui appelle `wsprintfA`, `lstrlenA` et `GetTickCount64`, puis `qsort`, l'enregistrement de la fonction passe de 255 571 à 69 711 instructions et de 20,3 s à 5,5 s ; il reste le code du programme lui-même. Le code d'un appel sauté n'est pas enregistré, ni les rappels qu'il fait vers le code suivi.
- Pendant l'enregistrement, les registres (AVX compris) sont lus en un seul appel système par instruction : environ 12 % plus rapide.

Par défaut (`TraceSetThreads current`), l'enregistreur fait avancer un thread pendant que les autres s'exécutent librement : l'ordre des instructions de plusieurs threads dans la trace est l'ordre observé par le débogueur. Pour savoir quand il est exact, l'enregistreur échantillonne tous les threads (temps CPU et registres en mode utilisateur) au début et à la fin de l'enregistrement, à chaque changement de contexte et toutes les 4096 instructions. Un intervalle pendant lequel aucun autre thread n'a exécuté de code a un ordre **exact** ; sinon l'ordre est **approximatif** et les threads qui ont tourné hors de la trace sont listés avec leurs cycles CPU : `tt.info`, infobulles des changements de contexte dans l'onglet Time Travel, membre `cpu` de l'export JSON, `$result1` / `$result2` de `TraceExport`. Les threads qui se sont seulement réveillés dans le noyau (les suspensions du débogueur réveillent les threads en attente) ne le rendent pas approximatif.

**`TraceSetThreads all`** fait avancer pas à pas tous les threads, y compris ceux créés pendant la trace : chaque instruction de chaque thread est enregistrée, dans l'ordre des événements de débogage, et écrite une fois exécutée (sa mémoire est lue après elle, quoi qu'aient fait les autres threads entre-temps). Sur un programme dont quatre workers incrémentent un compteur partagé par une instruction atomique, entre une section critique et une boucle d'attente active, les 800 incréments sont tous enregistrés et se suivent dans l'ordre global (0 → 1 → … → 800) en 64 bits ; en 32 bits (WoW64), 792 à 800 selon les exécutions, quelques appels d'un thread échappant à la trace. Les threads s'exécutent toujours en parallèle : deux threads qui exécutent une instruction au même moment sur deux processeurs ne sont ordonnés que par leurs événements de débogage, et la valeur lue après la première peut déjà contenir l'écriture de la seconde (environ un quart des incréments quand trois workers ne font rien d'autre, test `trace_all_threads`). Un thread qui perd son drapeau TF sans le signaler (`NtContinue` au démarrage d'un thread ou après une exception) est réarmé en quelques événements. `tid()` dans la condition d'arrêt permet de s'arrêter sur un thread. L'instrumentation binaire dynamique et Intel PT ne sont pas utilisés : Intel PT exige un processeur Intel et un pilote noyau.

---

## Émulateur : exécuter le code du débogué sans l'exécuter

L'**émulateur** ([`emulator/`](emulator/README.md), en anglais) intègre à Fathom [dumpulator](https://github.com/mrexodia/dumpulator), de mrexodia (l'auteur d'x64dbg). Le débogué est sauvegardé dans un minidump (la commande `minidump`) et son code s'exécute dans un émulateur (Unicorn) **à partir de cette copie** : la mémoire, les modules et le thread sont ceux du processus à l'instant où il est en pause, les **appels système de Windows sont implémentés en Python** (mémoire, fichiers, registre, événements, sections...), et les **exceptions** aussi (SEH, VEH, filtre d'exception non gérée). Le processus débogué **ne bouge pas**.

À quoi ça sert : appeler une routine de déchiffrement sur n'importe quelle entrée, récupérer la chaîne que calcule une fonction, voir ce que fait une fonction avec des arguments choisis, explorer une branche sans modifier le programme — et l'enregistrer en trace pour l'onglet Time Travel.

```
emu.call ntdll.RtlComputeCrc32, 0, str:hello, 5     // $result = 0x3610A686
emu.call mymodule.decrypt, buf:256, 0x140017000     // le tampon de 256 octets est écrit dans le journal après l'appel
emu.trace on
emu.call kernel32.lstrlenW, wstr:Fathom             // enregistré et ouvert dans Time Travel
emu.run 0, .10000                                   // le thread courant, 10 000 instructions plus loin
emu.script "C:\scripts\dechiffre.py", 0x140001000   // toute l'API de dumpulator depuis Python
```

| Commande | Effet |
|---|---|
| `emu.call adresse[, argument ...]` | appelle une fonction (convention d'appel du processus) ; `$result` = sa valeur de retour. Arguments : expressions, `str:TEXTE` / `wstr:TEXTE` (chaîne ANSI / UTF-16 allouée dans l'émulateur), `buf:TAILLE` (tampon à zéro, écrit dans le journal après l'appel), `bytes:HEX` |
| `emu.run [fin[, nombre]]` | fait avancer le thread courant jusqu'à `fin` (0 : aucune) ou `nombre` instructions ; `$result` = l'adresse d'arrêt, `$result2` = le code de sortie si le processus émulé s'est terminé (sinon -1) |
| `emu.script fichier.py[, argument ...]` | exécute un script Python avec `dp`, le `Dumpulator` du dump (`dp.call`, `dp.read`, `dp.write`, `dp.allocate`, `dp.regs`, appels système redéfinis avec `@syscall`...), et `args` ; `result["return"] = valeur` fixe `$result` |
| `emu.trace on\|off` | enregistre les instructions émulées dans une trace (`.trace64` / `.trace32`) ouverte dans l'onglet **Time Travel** et la vue des traces d'x64dbg ; `$result1` = le nombre d'instructions |
| `emu.syscalls on\|off` | écrit les appels système du code émulé, avec leurs arguments, dans le journal |
| `emu.strict on\|off` | s'arrête à un appel système ou une instruction qui manque à l'émulateur (off, par défaut : il échoue dans le programme et l'émulation continue) |
| `emu.cpus [n]` | processeurs de la machine émulée (0, par défaut : ceux de la machine du dump) |
| `emu.seed [n]` | graine de l'entremêlement des threads (1 par défaut : la même exécution à chaque fois) |
| `emu.screenshots on\|off` | enregistre des images (PNG) des fenêtres et boîtes de message du programme émulé ; leurs chemins dans le journal |
| `emu.network simulated\|real` | le réseau du programme émulé : simulé (par défaut) ou celui de l'hôte (seulement pour un programme de confiance) |
| `emu.dump [fichier]` | sauvegarde le débogué pour l'émulateur tout de suite (dans un fichier : conservé et utilisé jusqu'à `emu.unload`) |
| `emu.load fichier.dmp` / `emu.unload` | émule un fichier minidump (n'importe quel processus, même sans déboguer) / revient au débogué |
| `emu.info` | threads et modules du dump |
| `emu.setup` | installe les paquets Python de l'émulateur (`pip install -r emulator\requirements.txt`) |

Fonctionnement :

- Le dump est fait **quand il le faut et gardé jusqu'à ce que le débogué reprenne** (un pas, `run`) : plusieurs `emu.*` d'affilée émulent le même état. C'est un minidump complet (quelques centaines de Mo pour un gros processus), dans `%TEMP%\Fathom\emu`. Le thread émulé est le thread courant du débogueur.
- **Prérequis** : Python 3.9 ou plus récent dans le `PATH`, avec `unicorn` 2.1, `capstone` 5 et `pefile` — `emu.setup` les installe. L'émulateur tourne dans son propre processus Python ; sa sortie arrive dans le journal au fur et à mesure.
- **Traces** : les registres avant chaque instruction et la mémoire qu'elle a lue ou écrite, au format des traces d'x64dbg, donc `TraceExport` les convertit en JSON, CSV, Tenet ou `.ftt` comme toute trace enregistrée, et l'onglet Time Travel nomme les adresses quand le dump est le débogué. Environ 40 000 instructions par seconde pendant l'enregistrement (une boucle de 220 000 instructions : 5 s). Ce que les appels système écrivent en mémoire n'est pas dans la trace, seulement leur résultat dans les registres.
- **AVX** : Unicorn n'a pas d'AVX, mais le runtime C d'un processus qui tourne sur un processeur récent utilise AVX2 (`wcslen`, `memcpy`...). Fathom émule en Python les instructions AVX/AVX2 de ces routines ([`emulator/dumpulator/avx.py`](emulator/dumpulator/avx.py)), chacune vérifiée contre le processeur par `emulator/tests/test_avx.py`.
- **Threads et programmes entiers** : les threads que crée le code tournent aussi, **sur plusieurs processeurs émulés** (ceux de la machine du dump, `emu.cpus`) : les threads sur un processeur s'entremêlent toutes les quelques centaines d'instructions, dans un ordre tiré d'une graine fixe (`emu.seed`), donc une course entre deux threads se produit comme en natif, et la même exécution revient à chaque fois. Sections critiques, verrous SRW, variables de condition, événements, mutex, sémaphores, `WaitForMultipleObjects`, TLS statique, `Sleep` avec une horloge virtuelle (`GetTickCount` et `QueryPerformanceCounter` la suivent). Un programme tourne de son point d'entrée à sa sortie, démarrage et fin du runtime C compris (DLL chargées par `\KnownDlls`, sortie console, `RaiseException`). Le programme de test `src/tests/emulator_threads` (quatre threads qui se disputent une section critique, événements, mutex, sémaphore, variable de condition) donne dans l'émulateur le même résultat qu'en natif, en 64 et en 32 bits, et ses 1,5 million d'instructions enregistrées s'ouvrent dans Time Travel avec leurs cinq threads.
- **Fenêtres, réseau, autres processus, COM** : les fenêtres de user32 (classes, fenêtres, messages, minuteries sur l'horloge virtuelle, boîtes de message, dialogues) vivent dans un gestionnaire de fenêtres avec un utilisateur simulé qui répond OK aux dialogues et ferme les fenêtres quand le programme n'a plus rien à faire, et ce qu'elles montrent est **dessiné** : le programme peint par gdi32 comme en natif (`WM_PAINT`, texte, pinceaux, stylos, formes, bitmaps et sections DIB avec `BitBlt` / `StretchDIBits`, régions et découpage, tracés, contrôles), GDI+, OpenGL (1.x à 4.x, GLSL, extensions par `wglGetProcAddress`) et Direct3D 9, 10, 11 et 12 dessinent (un rastériseur logiciel vectorisé qui découpe les triangles contre le plan proche ; les shaders de Direct3D 9, toutes les étapes de Direct3D 10 / 11 / 12 (sommets, coque, domaine, géométrie, pixels, calcul, amplification et maillage ; DXBC et DXIL jusqu'au shader model 6.9, graphes de travail compris) et toutes les étapes de GLSL (sommets, tessellation, géométrie, fragments, calcul) tournent dans des interpréteurs, avec les dérivées et les niveaux de mip par quads de 2x2, le tessellateur de Direct3D 11, la sortie de flux (stream output), les contextes différés, les effets de Direct3D 10, le lancer de rayons de Direct3D 12 (géométrie procédurale, signatures racines locales, RayQuery, HitObject) et les textures BC1 à BC7 ; un lot de pixels est partagé entre plusieurs threads), et `emu.screenshots on` enregistre une image (PNG) de chaque fenêtre et boîte de message, son chemin dans le journal. Winsock, WinINet, WinHTTP et `URLDownloadToFile` atteignent un **réseau simulé** (les noms se résolvent en 192.0.2.x, les connexions réussissent, ce que le programme envoie est enregistré, HTTP répond 200), ou celui de l'hôte avec `emu.network real`. `CreateProcess` et `ShellExecute` lancent le **processus enfant** dans un émulateur à lui, un enfant de l'**autre architecture** compris (à partir du dump d'un petit programme hôte de cette architecture, fait une fois par le headless de Fathom), et les processus d'une exécution **tournent en même temps** : quand ils partagent de la mémoire, leurs instructions s'entremêlent sur les processeurs émulés comme celles des threads (une course entre eux se produit comme en natif), et dès que l'un attend un autre, les autres tournent. Ils se parlent donc comme en natif : **tubes** (anonymes, sur l'entrée et la sortie standard redirigées d'un enfant, ou nommés), **mémoire partagée** (sections), événements, mutex et sémaphores nommés, handles hérités et dupliqués, **messages Windows** (`FindWindow`, `PostMessage`, `SendMessage` qui attend le processus destinataire, `WM_COPYDATA`) ; `VirtualAllocEx`, `WriteProcessMemory`, `Get/SetThreadContext`, `ResumeThread`, `CreateRemoteThread`, les attentes et `GetExitCodeProcess` du parent agissent sur ses enfants, qui continuent après sa sortie. Les objets **COM en processus** tournent dans l'émulateur (leur DLL est chargée, `DllGetClassObject` et `IClassFactory::CreateInstance` appelés) ; un serveur hors processus et un RPC vers un service reçoivent la réponse « indisponible », enregistrée ; le gestionnaire de services (`CreateService`, `StartService`...) est émulé. Le **registre** et les **fichiers** sont lus sur l'hôte, et ce que le programme écrit reste dans l'émulateur (l'hôte ne change jamais) ; `LoadLibrary` charge n'importe quelle DLL du disque avec son `DllMain`. Le journal se termine par ce que le programme a fait (réseau, fenêtres et images, enfants, registre, fichiers, COM / RPC / services).
- **Ce qui manque à l'émulateur** ne l'arrête plus : un appel système qu'il n'implémente pas renvoie `STATUS_NOT_IMPLEMENTED` au programme, une instruction qu'il n'émule pas lève `STATUS_ILLEGAL_INSTRUCTION` dans le programme, comme sur un Windows ou un processeur qui ne les a pas, et la fin du journal les nomme. `emu.strict on` s'y arrête à la place.
- **Compte d'instructions** : `emu.run 0, n` exécute exactement `n` instructions, quel que soit le nombre de threads.
- **Limites** : les serveurs COM hors processus, les RPC vers des services et le shell au-delà de `ShellExecute` reçoivent la réponse « indisponible » (enregistrée)  ; les opérations du shader model 6.10 (une préversion qu'aucun compilateur ni pilote ne fait tourner ici : clusters, positions de triangles, matrices LinAlg) sont enregistrées comme non exécutées, et le rendu, en Python avec numpy sur quelques threads, reste bien plus lent qu'un GPU ; un enfant de l'autre architecture a besoin de `emulator\hosts` et du headless de Fathom de cette architecture ; AVX-512 n'est pas émulé. x64dbg émule les processus 64 bits, x32dbg les 32 bits.
- dumpulator est intégré avec des modifications pour unicorn 2.1 et Python 3.12+ (les versions qu'il impose ne s'y installent plus), `NtOpenThreadToken` et les instructions AVX : voir [`emulator/README.md`](emulator/README.md). `python emulator\fathom_emu.py` marche aussi seul sur n'importe quel minidump (`call app.dmp kernel32!lstrlenA str:salut`), et `Debugger.emulator()` de pybridge renvoie un `Dumpulator` du débogué.

Test automatique : [`extracmds/tests/test_emulator.py`](extracmds/tests/test_emulator.py) (headless, x64 et x32 : arguments, appels système, trace dans Time Travel, script, fichier minidump après la fin de la session, un programme multithread de son point d'entrée à sa sortie, le programme `src/tests/emulator_world` : une course sur plusieurs processeurs, une instruction qui manque à l'émulateur, une fenêtre qui se peint (son image vérifiée pixel par pixel), le réseau simulé, des processus enfants, des tubes et de la mémoire partagée entre processus qui tournent en même temps, un tube nommé, un enfant de l'autre architecture, `ShellExecuteEx`, un objet COM en processus, fichiers et registre, deux processus en course sur de la mémoire partagée, des messages Windows entre processus, bitmaps, régions et tracés vérifiés pixel par pixel, formes GDI+, images d'OpenGL, de Direct3D 9 et de Direct3D 11 (shaders et textures) relues et vérifiées, puis des triangles découpés par le plan proche, OpenGL 3.3 avec GLSL, les shaders de Direct3D 9, Direct3D 10, les shaders de géométrie, de tessellation et de calcul de Direct3D 11, un maillage de 20000 triangles, Direct3D 12, des textures BC6H / BC7, les dérivées et les niveaux de mip, le tessellateur avec un niveau par arête, la sortie de flux, les contextes différés, les shaders de calcul, de géométrie et de tessellation d'OpenGL 4.3, un effet Direct3D 10, des shaders DXIL et le lancer de rayons de Direct3D 12, puis les shaders de coque, de domaine et de géométrie du shader model 6, les opérations DXIL plus rares, un shader d'intersection avec signatures racines locales et RayQuery, des shaders d'amplification et de maillage, un graphe de travail et le shader model 6.9 ; débogué inchangé). Dans l'interface, la trace émulée s'ouvre dans l'onglet Trace d'x64dbg (instructions, registres, mémoire, symboles et lignes de source) et dans l'onglet Time Travel (une chronologie par thread).

> À ne pas confondre avec `pybridge/examples/emulate_dump.py`, un émulateur de processeur seul qui avance pas à pas dans un dump et s'arrête au premier appel système (il sert le dump par le protocole pybridge).

---

## Exécution symbolique : Triton

Le **plugin Triton** ([`triton/`](triton/README.md)) apporte dans Fathom [Triton](https://github.com/JonathanSalwan/Triton), la bibliothèque d'analyse binaire dynamique de Jonathan Salwan et Quarkslab : **exécution symbolique**, **analyse de teinte** (taint) et **résolution SMT** avec Z3, par les commandes `sym.*` et `taint.*`, sur deux sources :

- **le débogué** (`sym.start`) : Triton part de l'état du thread courant, lit la mémoire du processus quand il en a besoin et exécute le code lui-même : le débogué **ne bouge pas** ;
- **une trace enregistrée** (`sym.trace`, `.ftt` ou `.trace64` / `.trace32`) : chaque instruction enregistrée est rejouée avec les valeurs de la trace, ce qui permet d'analyser après coup une exécution réelle (appels d'API et autres threads compris).

À quoi ça sert : trouver **l'entrée qui passe une vérification** (numéro de série, mot de passe, valeur magique) et l'écrire dans le débogué, prendre **l'autre côté d'un branchement**, voir **d'où vient une valeur**, **simplifier une arithmétique obscurcie** (MBA), **suivre une entrée** dans le code.

```
sym.start                       // à l'entrée de check(serial) : une session Triton sur l'état courant
sym.mem rcx, 8, serial          // les 8 octets du numéro de série deviennent des variables serial_0..serial_7
sym.run                         // Triton exécute check() jusqu'à son retour (le débogué reste ici)
sym.solve eax, 1                // Z3 : le numéro pour lequel check() renvoie 1 (octets et texte dans le journal)
sym.apply                       // écrit dans le débogué : check() exécutée nativement renvoie maintenant 1
sym.branches                    // les branchements qui dépendaient du numéro ; sym.flip n prend l'autre côté
sym.synth eax                   // (x ^ y) + 2 * (x & y)  ->  (y + x) & 0xffffffff
```

| Commande | Effet |
|---|---|
| `sym.start` / `sym.trace fichier[, seq[, thread]]` / `sym.stop` / `sym.info` | une session sur le thread courant du débogué / sur une trace à partir de l'instruction `seq` / fin / état |
| `sym.reg reg[, nom]`, `sym.mem adresse, taille[, nom]`, `sym.memvar adresse, taille[, nom]` | rend symbolique un registre, des octets (une variable chacun) ou un entier de 1 à 8 octets |
| `taint.reg`, `taint.mem`, `untaint.*`, `sym.concrete` | teinte / déteint, retire les données symboliques |
| `sym.step [nombre]`, `sym.run [fin[, nombre]]` | exécute ; sans `fin`, jusqu'au retour de la fonction courante (débogué) ou à la fin de la trace du thread |
| `sym.expr`, `sym.smt`, `sym.synth`, `sym.slice` `reg \| adresse[, taille]` | l'expression (simplifiée par Z3), en SMT-LIB, synthétisée, les instructions dont elle provient |
| `sym.vars`, `sym.branches`, `sym.list [nombre]`, `sym.mark` / `sym.unmark` | variables symboliques, branchements symboliques, instructions sur des données symboliques / teintées (commentées dans le désassemblage) |
| `sym.solve cible, valeur[, taille]`, `sym.flip n`, `sym.reach adresse`, `sym.apply` | l'entrée pour une valeur / l'autre côté d'un branchement / une adresse ; l'écrit dans le débogué |

Les arguments sont évalués dans l'état de la session (les registres après `sym.run` ou à la position de la trace). Triton s'arrête aux appels système (il n'exécute pas le noyau : pour un programme entier, l'émulateur est l'outil) et suit un seul thread. Z3 4.16 est livré en `libz3.dll` à côté du plugin ; Triton, Capstone 5.0.7 et Z3 sont compilés et épinglés par [`triton/build.ps1`](triton/build.ps1). Documentation complète (en anglais) : [`triton/README.md`](triton/README.md).

Le plugin trouve et simplifie aussi le **code virtualisé** dans les traces (`vm.detect`, `vm.simplify` : la méthode de VMHunt), voir la section suivante.

Test automatique : [`triton/tests/test_triton.py`](triton/tests/test_triton.py) (headless, x64 et x32, 20 assertions : un numéro de série résolu, appliqué et accepté nativement, un branchement inversé, une expression MBA synthétisée en `x + y`, la teinte, une trace enregistrée rejouée et son numéro résolu).

## Décompilateurs, dévirtualisation, angr, miasm, remill, Binary Ninja

D'autres analyses du débogué **tel qu'il est maintenant** (dépaqueté, déchiffré, modifié), depuis le débogueur :

- **Le décompilateur de Ghidra** ([`decompiler/`](decompiler/README.md), `Decompiler.dp64` / `.dp32`) : `decompile [adresse[, fichier]]` transforme la fonction en C avec le décompilateur natif de [Ghidra](https://github.com/NationalSecurityAgency/ghidra). Le code est lu dans la mémoire du processus, la mémoire en lecture seule est constante (les appels par la table d'imports vont à leur cible), les fonctions portent les noms des symboles et étiquettes d'x64dbg, et les chaînes littérales sont commentées en fin de ligne. `decompile.raw` affiche le p-code de Ghidra. Rien à installer : le décompilateur et les spécifications sleigh de Ghidra sont intégrés.
- **Dévirtualisation de VMProtect** ([`novmp/`](novmp/README.md)) : `novmp [module]` lit l'image du module en mémoire et la donne à [NoVmp](https://github.com/can1357/NoVmp), qui remonte les machines virtuelles de VMProtect 3.0 – 3.5 en [VTIL](https://github.com/vtil-project/VTIL-Core) optimisé (`vtildump` les affiche dans le journal). NoVmp ne traite que le x64 ; Fathom le porte en x86 (`NoVmp32.exe`, utilisé pour les modules de x32dbg ; la base du pointeur d'instruction virtuel se règle : `novmp module, vipbase:0`). Sur la démo officielle de VMProtect 3.11, NoVmp32 trouve les entrées de VM ; les VM de VMProtect 3.6 – 3.11 ont une autre structure d'entrée, que NoVmp détecte et signale clairement (`NOVMP_DEBUG_VMENTER=1` l'affiche) au lieu de la traduire de travers.
- **Code virtualisé dans une trace** (la méthode de [VMHunt](https://github.com/s3team/VMHunt), dans le plugin Triton) : `vm.detect` trouve les portions virtualisées d'une trace enregistrée (sauvegarde du contexte, répartiteur, gestionnaires, restauration), `vm.simplify n` en rejoue une avec des registres symboliques et affiche ce qu'elle calcule, simplifié et synthétisé : sur la machine virtuelle du test, `(eax_in ^ 0x5a) + ebx_in * 3`.
- **angr** ([`analysis/`](analysis/README.md)) : `angr.symbolic adresse, taille`, puis `angr.explore cible[, à éviter ...]` explore tous les chemins depuis l'état du thread courant (un minidump du processus chargé dans [angr](https://github.com/angr/angr)) et résout l'entrée qui atteint la cible ; `angr.apply` l'écrit dans le débogué.
- **remill** ([`remill/`](remill/README.md)) : `remill.lift [adresse[, taille]]` traduit la fonction (lue en mémoire) en IR LLVM avec [remill](https://github.com/lifting-bits/remill), compilé pour Windows par Fathom (remill vise Linux et macOS) : l'IR est dans le journal et dans un fichier `.ll`, prêt pour les outils de LLVM.
- **rev.ng** ([`revng/`](revng/README.md)) : `revng.decompile [adresse]` décompile la fonction avec [rev.ng](https://rev.ng), qui ne tourne que sous Linux et que Fathom porte sous Windows en natif (les traducteurs de QEMU en bibliothèque sans linux-user, LLVM 16, rev.ng lié dans son module Python). Le module est lu en mémoire, analysé une fois (fonctions, prototypes, types), et le C de la fonction est dans le journal. Modules x64 et x86. rev.ng est livré dans son archive (`Fathom_<version>_revng.zip`, à extraire à côté de `Fathom.exe` : `revng\`, autonome, environ 340 Mo).
- **Binary Ninja** ([`binja/`](binja/README.md)) : `binja [adresse]` ouvre le module, lu en mémoire à son adresse de chargement, dans [Binary Ninja](https://binary.ninja) à l'adresse (toutes les éditions, Free comprise) ; avec une édition payante, le plugin `fathom_binja` synchronise les deux par pybridge : la vue suit le débogué, adresses montrées dans Fathom, points d'arrêt et « exécuter jusqu'ici » depuis Binary Ninja, traces de Fathom surlignées.
- **miasm** ([`analysis/`](analysis/README.md)) : `miasm.ir [adresse]` traduit une fonction dans l'IR de [miasm](https://github.com/cea-sec/miasm), `miasm.symb [adresse[, fin]]` l'exécute symboliquement et affiche ce qu'elle calcule, simplifié (`(x ^ y) + 2 * (x & y)` : `RAX = (RCX & RDX) * 2 + (RCX ^ RDX)`).

angr et miasm demandent Python 3.10+ avec leurs paquets : `analysis.setup` les installe (miasm lui-même est intégré).

Aussi en dépôts séparés, avec leurs releases : [fathom-novmp](https://github.com/EGKrb/fathom-novmp) (NoVmp / NoVmp32 et vtildump), [fathom-remill](https://github.com/EGKrb/fathom-remill) (remill pour Windows), [fathom-analysis](https://github.com/EGKrb/fathom-analysis) (les scripts angr et miasm, sur n'importe quel minidump) et [fathom-binja](https://github.com/EGKrb/fathom-binja) (le plugin Binary Ninja). Ils sont développés ici et publiés là-bas.

Tests automatiques (headless, x64 et x32) : [`decompiler/tests/test_decompiler.py`](decompiler/tests/test_decompiler.py) (une boucle, un switch et ses chaînes, des appels d'API par la table d'imports), [`extracmds/tests/test_tools.py`](extracmds/tests/test_tools.py) (miasm, angr qui résout un numéro de série accepté nativement, NoVmp et NoVmp32 sur un module lu en mémoire, une routine VMProtect affichée par `vtildump`, remill sur une fonction, `binja` avec un Binary Ninja factice, rev.ng qui décompile une fonction), [`binja/tests/test_binja.py`](binja/tests/test_binja.py) (la synchronisation du plugin face à un vrai Fathom), `emulator_test` de NoVmp (son émulateur natif, x64 et x86), et la partie `vm.*` de [`triton/tests/test_triton.py`](triton/tests/test_triton.py).

[Z3](https://github.com/Z3Prover/z3) est le solveur du plugin Triton (la version officielle 4.16, celle que Triton épingle), et Capstone y est en version 5 (Triton l'exige ; `capstone-next` est la version 6).

---

## pybridge : piloter x64dbg depuis Python

```
 script Python                         x64dbg / headless.exe
 x64dbg_bridge.py   ── TCP 127.0.0.1:27041 ──►  plugin pybridge.dp64
 (bibliothèque          une requête JSON          (thread serveur)
  standard seule)       par ligne                      │
                    ◄── une réponse JSON ──    API x64dbg : commandes,
                                               expressions, mémoire...
```

Le plugin ouvre un serveur **uniquement sur l'interface locale** (127.0.0.1) et traduit chaque requête en appel à l'API d'x64dbg. Le client Python n'a **aucune dépendance**.

### Démarrage

- `Debugger.launch(dossier_x64dbg)` démarre `headless.exe` (ou `x64dbg.exe` avec `gui=True`) avec la variable `X64DBG_PYBRIDGE_PORT`, ce qui démarre automatiquement le serveur, puis s'y connecte. À la fin du bloc `with`, le débogage est arrêté et `headless.exe` fermé.
- Dans un x64dbg déjà ouvert : commande `pybridge.start` (port décimal facultatif, 27041 par défaut), puis `Debugger()` en Python. Aussi : `pybridge.stop`, `pybridge.status`.

```python
import sys
sys.path.insert(0, r"C:\Outils\x64dbg\release\pybridge\python")
from x64dbg_bridge import Debugger

with Debugger.launch(r"C:\Outils\x64dbg\release") as dbg:
    dbg.init(r"C:\Windows\System32\whoami.exe")           # démarre et attend la 1re pause

    # Breakpoint qui journalise sans s'arrêter
    dbg.set_breakpoint("kernelbase.CreateFileW", break_=False,
                       log="CreateFileW({utf16@arg.get(0)})")

    # Breakpoint qui s'arrête
    dbg.set_breakpoint("kernelbase.LoadLibraryExW")
    if dbg.run(wait=True, timeout=10):
        print("DLL :", dbg.read_string(dbg.eval("arg.get(0)"), wide=True))
        print(dbg.regs())
        for ins in dbg.disasm("cip", 5):
            print(hex(ins["address"]), ins["text"])
```

### API Python

| Catégorie | Méthode | Description |
|---|---|---|
| Session | `Debugger.launch(dir, arch="x64", gui=False, port, log_file)` | démarre x64dbg et se connecte |
| | `init(path, arguments, cwd)` / `attach(pid)` | lance / s'attache, attend la première pause |
| | `stop()`, `close()` | arrête le débogage / ferme la session |
| Exécution | `run(wait=False, timeout=10, pass_exceptions=False)` | reprend ; avec `wait=True`, renvoie `True` si le programme s'est de nouveau arrêté |
| | `pause()`, `step_into()`, `step_over()`, `step_out()` | renvoient `True` une fois en pause |
| | `wait(timeout)`, `state()` | attente de pause ; état `{debugging, running, cip}` |
| Registres | `regs()`, `reg(nom)`, `set_reg(nom, valeur)` | registres généraux, IP, drapeaux |
| Mémoire | `read(adr, taille)`, `write(adr, octets)`, `read_ptr(adr)`, `read_string(adr, wide)`, `is_valid(adr)` | lecture/écriture (jusqu'à 64 Mo par appel) |
| | `disasm(adr, n)` | `n` instructions : adresse, taille, texte, octets |
| Breakpoints | `set_breakpoint(adr, condition, log, command, break_, singleshot)` | logiciel, avec condition, texte de log et commande automatiques |
| | `set_hardware_breakpoint(adr, kind="x"/"w"/"r", size)`, `delete_breakpoint(adr)` | matériel ; suppression (tous si `adr` absent) |
| Traces | `trace(fichier, condition, max_steps, step_over)` | trace (et enregistre) jusqu'à la condition |
| | `export_trace(trace, sortie, fmt, slide)` | `TraceExport` (`fmt` : `json`, `csv`, `tenet`, `frinet`, `ftt`) ; renvoie le nombre d'instructions |
| Émulation | `emulate_call(adr, *args, trace=False)` | `emu.call` : la fonction appelée dans l'émulateur ; renvoie sa valeur de retour (`args` : entiers, expressions, `"str:..."`, `"wstr:..."`, `"buf:N"`, `"bytes:HEX"`) |
| | `emulate_run(fin=0, nombre=0, trace=False)` | `emu.run` : le thread courant poursuivi dans l'émulateur ; renvoie l'adresse d'arrêt |
| | `minidump(chemin)`, `emulator(dump=None, **options)` | sauvegarde le débogué dans un minidump ; `emulator()` en renvoie un `dumpulator.Dumpulator` dans le processus du client |
| Commandes | `cmd("texte")`, `command(nom, *args)`, `eval(expr)`, `result` | toute commande x64dbg ; `command()` échappe les arguments texte |

Les adresses et valeurs acceptent un **entier** ou une **expression x64dbg** (chaîne) ; les résultats sont des entiers Python. Les erreurs (mémoire illisible, expression invalide, commande refusée) lèvent `BridgeError` avec le message d'x64dbg.

### Exemples fournis ([`pybridge/examples/`](pybridge/examples))

| Script | Ce qu'il fait |
|---|---|
| `api_monitor.py cible.exe` | journalise les appels à CreateFileW, CreateProcessW, LoadLibraryExW, VirtualAlloc, VirtualProtect, WriteProcessMemory... avec leurs arguments, sans arrêter le programme |
| `trace_to_json.py cible.exe --steps 20000 --out dossier` | trace depuis le point d'entrée, exporte en JSON/CSV, affiche les instructions les plus exécutées et le nombre d'écritures mémoire |
| `dump_memory.py --target cible.exe --addr "mod.base(cible.exe)" --size "mod.size(cible.exe)" --out m.bin` | sauvegarde une zone mémoire (ou `--pid` pour un processus existant, `--at` pour attendre une adresse) |
| `step_log.py cible.exe --count 50` | pas à pas en affichant les registres modifiés par chaque instruction |

Les exemples utilisent l'x64dbg indiqué par `--x64dbg` ou la variable d'environnement `X64DBG_DIR`.

### Protocole (pour d'autres langages)

Une requête JSON par ligne, une réponse JSON par ligne :

```
→ {"id": 1, "method": "mem_read", "params": {"addr": "rsp", "size": 16}}
← {"id": 1, "result": "00000000000000009512A857F77F0000"}
→ {"id": 2, "method": "mem_read", "params": {"addr": 0, "size": 4}}
← {"id": 2, "error": "cannot read memory"}
```

Méthodes : `ping`, `cmd`, `eval`, `state`, `wait`, `run`, `pause`, `step_into`, `step_over`, `step_out`, `stop`, `regs`, `reg_get`, `reg_set`, `mem_read`, `mem_write`, `mem_valid`, `disasm` (paramètres dans [`pybridge/src/api.cpp`](pybridge/src/api.cpp)).

### Sécurité et limites

- **Tout programme local qui se connecte au port contrôle le débogueur** (lecture/écriture de la mémoire du processus débogué, exécution de commandes). Le serveur n'écoute que sur 127.0.0.1 et ne démarre que sur demande ; ne le laissez pas actif inutilement.
- Un seul client à la fois ; les requêtes sont traitées dans l'ordre.
- `run()` ne bloque pas par défaut : utilisez `wait=True` ou `wait()` avec un délai, puis `pause()` si besoin.

---

## Modèles pour headless.exe

[`pybridge/headless/run-headless.ps1`](pybridge/headless/run-headless.ps1) exécute un script x64dbg sans interface :

```powershell
.\pybridge\headless\run-headless.ps1 -Template api-log -Target C:\Windows\System32\whoami.exe
.\pybridge\headless\run-headless.ps1 -Template trace-export -Target .\programme.exe -Arch x32
```

Fonctionnement du lanceur :

1. remplace dans le modèle `%TARGET%` (chemin complet de la cible), `%MODULE%` (nom du fichier), `%OUT%` (dossier de sortie) et `%BITS%` (64 ou 32) ;
2. crée `out\<modèle>-<date>\` avec le script final (`script.txt`) et un **dossier de configuration isolé** : les `settingset` du modèle ne modifient pas votre configuration ;
3. lance `headless.exe -userdir ... -cf script.txt`, affiche et enregistre le journal (`headless.log`) ;
4. ferme `headless.exe` quand le script affiche `[template] done`, quand le programme débogué s'est terminé, ou après `-TimeoutSeconds` (300 par défaut).

| Modèle | Ce qu'il fait | Fichiers produits |
|---|---|---|
| `api-log` | journalise les appels d'API et leurs arguments sans jamais arrêter le programme | `headless.log` (lignes `[api]`) |
| `trace-export` | trace 50 000 instructions depuis le point d'entrée et les exporte | `trace.trace64`, `trace.json`, `trace.csv`, `trace.log` (Tenet) |
| `dump-module` | sauvegarde le module principal et un minidump complet au point d'entrée | `<module>.mem`, `process.dmp` |
| `break-on-file` | s'arrête seulement quand le programme ouvre un fichier `.txt`, puis journalise l'appel, sauvegarde la pile et écrit un minidump ; se termine proprement si aucun fichier ne correspond | `stack.bin`, `at_createfile.dmp` |

Les modèles lisent les arguments avec `arg.get(n)` et fonctionnent donc en 64 et 32 bits. Les breakpoints sont posés dans `kernelbase.dll`, où se trouve le vrai code des API (les programmes récents l'appellent directement ; `kernel32` ne fait que rediriger).

### Écrire son propre modèle

Un modèle est un script x64dbg ordinaire :

```
// commentaire (ou ;)
settingset Events, EntryBreakpoint, 1             // s'arrêter au point d'entrée
init "%TARGET%"                                    // lancer la cible, le script attend la pause
bp kernelbase.VirtualProtect
bpcond kernelbase.VirtualProtect, "arg.get(2) == 40"   // ne s'arrêter que si PAGE_EXECUTE_READWRITE
erun                                               // continuer ; le script reprend à la prochaine pause
cmp $pid, 0                                        // $pid = 0 : le programme s'est terminé
je fin
savedata "%OUT%\zone.bin", arg.get(0), arg.get(1)
fin:
log "[template] done"
```

Format des textes de log : `{x:rax}` hexadécimal, `{d:...}` décimal, `{p:...}` pointeur, `{a:addr}` adresse avec symbole, `{utf16@addr}` / `{utf8@addr}` chaîne, `{i:addr}` instruction. Voir la [documentation des commandes](https://help.x64dbg.com/en/latest/commands/).

---

## Commandes ajoutées et dumps mémoire

Le plugin **ExtraCmds** ([`extracmds/`](extracmds/src/extracmds.cpp)) ajoute ces commandes, utilisables dans la barre de commande, les scripts, `headless.exe` et pybridge :

| Commande | Effet |
|---|---|
| `.init fichier.exe[, arguments[, dossier]]` | lance et débogue le programme (identique à `init`) |
| `.init fichier.dmp` | ouvre le dump dans le **visualiseur de dumps** (voir ci-dessous) ; un crash dump **noyau** (`MEMORY.DMP`) ou une image `.raw`/`.mem`/`.vmem` s'ouvre dans **wineye** ([HvKdFusion](#hvkdfusion--débogage-noyau-et-forensics-mémoire-hyper-v)) |
| `.save` | sauvegarde la base de données du programme (commentaires, labels, breakpoints) — comme `dbsave` |
| `.save fichier.dmp` | écrit un minidump complet du processus — comme `minidump` |
| `.save fichier.dd64` | sauvegarde la base de données dans ce fichier — comme `dbsave fichier` |
| `.save fichier, adresse` | sauvegarde la zone mémoire qui contient l'adresse (`mem.base` / `mem.size`) |
| `.save fichier, adresse, taille` | sauvegarde une plage mémoire — comme `savedata` (taille en hexadécimal : `100` = 256 octets, `.256` en décimal) |
| `scylla_hide.status` | affiche si ScyllaHide est actif, son profil et les profils disponibles |
| `scylla_hide.enable [profil]` | active ScyllaHide : profil donné (nom exact ou début du nom, ex. `vmprotect`, `basic`), sinon le dernier profil actif, sinon « Basic » |
| `scylla_hide.disable` | désactive ScyllaHide (profil « Disabled ») |
| `hvkd [action [options]]`, `hvkd.log`, `hvkd.check`, `wineye` | HvKdFusion, voir [plus bas](#hvkdfusion--débogage-noyau-et-forensics-mémoire-hyper-v) |

```
scylla_hide.enable basic
.init "C:\cible.exe"
.save "C:\out\cible.dmp"
.save "C:\out\pile.bin", rsp
.init "C:\out\cible.dmp"
```

Fonctionnement :

- **ScyllaHide** ne lit sa configuration qu'au chargement. `scylla_hide.enable/disable` modifie `plugins\scylla_hide.ini` (`CurrentProfile`) puis recharge le plugin (`plugunload` + `plugload`) : le profil s'applique immédiatement, sans redémarrer x64dbg. Les protections sont injectées **au démarrage ou à l'attachement** du programme : si un programme est déjà en cours de débogage, relancez-le pour qu'il prenne le nouveau profil.
- **Un dump n'est pas un processus** : x64dbg débogue des programmes en cours d'exécution et ne peut pas « charger » un `.dmp` (pas d'exécution, de pas à pas ni de breakpoints sur un dump). `.init fichier.dmp` ouvre donc le **visualiseur de dumps** `x64\minidump.exe`, issu du code officiel d'x64dbg (`src/cross/minidump`) : carte mémoire, désassemblage, vue hexadécimale, threads et registres. Il lit les dumps de processus 32 et 64 bits et peut aussi être lancé seul (`minidump.exe fichier.dmp`). Pour analyser un dump avec des commandes et des scripts, utilisez WinDbg (`cdb -z fichier.dmp`).

---

## HvKdFusion : débogage noyau et forensics mémoire Hyper-V

x64dbg s'arrête au mode utilisateur. **HvKdFusion** ([`hvkdfusion/`](hvkdfusion/README.md), fusionné depuis [EGKrb/HvKdFusion](https://github.com/EGKrb/HvKdFusion)) couvre le noyau et la mémoire entière d'une machine — une **VM Hyper-V live** ou une **image mémoire** — en reliant LiveCloudKd (`hvlib.dll`/`hvmm.sys`, mémoire des invités Hyper-V), WinDbg/kd, MemProcFS + LeechCore (+ Dokany pour monter la mémoire en lecteur) et ntoseye, sous une seule CLI Python. Il apporte **wineye**, un REPL natif façon WinDbg (`ps`, `lm`, `!process`, `x nt!...`, `db/dq`, `dt`, `!vad`, `!handle`, `s`) sur `vmm.dll`, sans WSL ni Dokany, avec gel/reprise/registres sur une VM live.

| Commande (ExtraCmds) | Effet |
|---|---|
| `hvkd` | menu interactif dans une console : choix de la VM puis déboguer (WinDbg/kd/EXDI), dump, lire, écrire, reprendre, ntoseye, monter |
| `hvkd action [options]` | n'importe quelle action HvKdFusion dans une console, ex. `hvkd dump --vm 0 --out C:\dumps\vm0.raw`, `hvkd debug --vm 0 --engine windbg` |
| `hvkd.log action [options]` | idem, sans fenêtre, sortie dans le journal d'x64dbg : `hvkd.log list`, `hvkd.log read --vm 0 --addr 0x1000 --size 0x100` |
| `hvkd.check [--deep]` | tableau de préparation de chaque composant, dans le journal |
| `wineye fichier` / `wineye --live --vm 0` | wineye sur un dump noyau / une image brute, ou sur une VM live |

```
hvkd.check
hvkd dump --vm 0 --type linear --out C:\dumps\vm0.raw
.init C:\dumps\vm0.raw
wineye --live --vm 0
```

Les opérations mémoire sur une VM exigent les droits administrateur : `hvkd` demande l'élévation (UAC) quand il le faut. Les composants tiers ne sont **pas inclus** (licences, pilotes noyau) : installez-les ou compilez-les, puis lancez une fois `python hvkdfusion.py setup` — voir [hvkdfusion/README.md](hvkdfusion/README.md) (en anglais). Nécessite Python 3.11+ sur le `PATH`.

### Onglet HvKdFusion (GUI)

Quand ExtraCmds est compilé avec Qt (par défaut pour le plugin 64 bits, voir [Compilation](#compiler-soi-même)), il ajoute un onglet dockable **HvKdFusion** (menu Affichage / barre d'onglets), sans rien à taper :

- un **sélecteur de VM** + **Rafraîchir**, et **Ouvrir un dump…** pour cibler un crash dump / une image brute plutôt qu'une VM live ;
- un **panneau d'état** des composants (pastilles vert/orange/rouge, le tableau `check`) avec les fonctionnalités disponibles et un bouton **Setup** ;
- un **tableau de processus** triable (`ps`) ; sélectionner un processus remplit les onglets **Modules / VAD / Handles** (`lm` / `!vad` / `!handle`) ;
- des boutons **Dump / Debug / Monter** qui agissent sur la VM sélectionnée (élévation demandée au besoin).

L'onglet pilote les mêmes outils Python via leur sortie JSON et ne bloque jamais x64dbg (tous les appels sont asynchrones). 64 bits uniquement ; sans Qt, le plugin reste en lignes de commande seules.

---

## Plugins inclus

| Plugin | Version | Rôle | Utilisation |
|---|---|---|---|
| **ScyllaHide** | v1.4 | cache le débogueur aux techniques anti-debug (PEB, `NtQueryInformationProcess`, timings, exceptions...) en injectant des hooks dans le processus | menu *Plugins > ScyllaHide > Options*. Profil actif par défaut : « VMProtect x86/x64 » ; passez à « Basic » si un programme non protégé se comporte mal |
| **xAnalyzer** | 2.5.12 | analyse statique : reconnaît les appels d'API et commente leurs arguments dans le désassemblage, détecte boucles et fonctions | menu *Plugins > xAnalyzer* ou clic droit dans le désassemblage, ou commandes `xanal selection` / `xanal function` / `xanal module` (`xanalremove ...` pour effacer) ; définitions d'API dans `plugins\apis_def\` |
| **OllyDumpEx** | v1.86 | dump d'un processus en fichier PE exécutable (pour les programmes compressés/protégés), en complément de Scylla intégré | menu *Plugins > OllyDumpEx* |
| **pybridge** | 1 | pilotage depuis Python | voir [pybridge](#pybridge--piloter-x64dbg-depuis-python) |
| **ExtraCmds** | 1 | commandes `.init`, `.save`, `scylla_hide.*` | voir [Commandes ajoutées](#commandes-ajoutées-et-dumps-mémoire) |

OllyDumpEx est un logiciel gratuit à code fermé **sans licence de redistribution** : il n'est donc pas inclus dans la release. [`install-plugins.ps1`](plugins/install-plugins.ps1) le télécharge depuis le site de son auteur.

```powershell
.\install-plugins.ps1                              # les trois plugins (ignore ceux déjà installés)
.\install-plugins.ps1 -Plugins OllyDumpEx          # un seul
.\install-plugins.ps1 -X64dbgDir C:\x64dbg\release -Force   # autre dossier, réinstallation
```

Chaque fichier téléchargé est vérifié par son **empreinte SHA-256** (liste dans [`plugins/THIRD-PARTY.md`](plugins/THIRD-PARTY.md)) : si un fichier a changé chez son auteur, l'installation s'arrête au lieu d'installer un fichier inconnu. Le script utilise `curl.exe -4` (IPv4), plus fiable que `Invoke-WebRequest` sur les réseaux où IPv6 est défaillant.

> Si vous installez xAnalyzer à la main : les définitions d'API doivent être dans `plugins\apis_def\`, sinon le plugin refuse de se charger (*« Failed to locate API definitions files »*).

---

## Compiler soi-même

Prérequis : Windows 10/11, **Visual Studio 2022** (charge de travail C++), **CMake** ≥ 3.15, **Ninja**, **git**, Python 3 (pour les tests).

```bat
git clone --recursive https://github.com/EGKrb/Fathom
cd x64dbg

:: 64 bits, dans un terminal « x64 Native Tools » (ou après vcvars64.bat)
cmake -B build64 -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_UNITY_BUILD=ON -DCMAKE_UNITY_BUILD_BATCH_SIZE=6
cmake --build build64

:: 32 bits, dans un terminal « x86 Native Tools » (ou après vcvars32.bat)
cmake -B build32 -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_UNITY_BUILD=ON -DCMAKE_UNITY_BUILD_BATCH_SIZE=6
cmake --build build32
```

Les exécutables sont produits dans `bin\x64` et `bin\x32` (Qt et les dépendances sont copiés automatiquement).

```bat
:: Plugins pybridge et ExtraCmds (même terminal que l'architecture voulue ; build-x32 en 32 bits) ;
:: le plugin Triton : powershell -File triton\build.ps1 -Sdk <dossier pluginsdk> (les deux architectures)
:: le plugin Decompiler : powershell -File decompiler\build.ps1 -Sdk <dossier pluginsdk>
:: NoVmp et vtildump (64 et 32 bits) : powershell -File novmp\build.ps1
:: remill (LLVM 17 et ses dépendances d'abord : des heures) : powershell -File remill\build.ps1
:: rev.ng pour Windows (MSYS2 ; LLVM 16 d'abord : des heures) : powershell -File revng\build.ps1
cd pybridge
cmake -B build-x64 -G Ninja -DCMAKE_BUILD_TYPE=Release -DX64DBG_SDK=<dossier pluginsdk>
cmake --build build-x64
cd ..\extracmds
cmake -B build-x64 -G Ninja -DCMAKE_BUILD_TYPE=Release -DX64DBG_SDK=<dossier pluginsdk> -DCMAKE_PREFIX_PATH=%CD%\..\build64\_deps\qt5-src
cmake --build build-x64
```

Par défaut, les plugins utilisent le `pluginsdk` d'un x64dbg installé avec winget ; sinon, indiquez celui d'une release (`-DX64DBG_SDK=...\pluginsdk`). Pour ExtraCmds, `-DCMAKE_PREFIX_PATH=<dépôt>\build64\_deps\qt5-src` (le Qt 5.12.12 d'x64dbg, présent après le build 64 bits) active l'**onglet HvKdFusion** ; sans lui, ou en 32 bits, ExtraCmds se compile en lignes de commande seules.

```powershell
# Visualiseur de dumps (terminal « x64 Native Tools », après la compilation 64 bits dont il réutilise Qt)
.\tools\build-minidump-viewer.ps1                    # produit build-cross-x64\minidump.exe, à copier dans bin\x64
```

```powershell
.\plugins\install-plugins.ps1                       # plugins tiers dans bin\x32 et bin\x64
py src\tests\run.py --arch x64                       # suite de tests (dont trace_export)
python hvkdfusion\tests\test_servers.py              # tests protocolaires wineye MCP/DAP/gdb
.\tools\make-release.ps1 -Version 1.11.0              # archives de release dans dist\ (stage aussi release\)
.\tools\build-installer.ps1 -Version 1.11.0           # dist\Fathom_1.11.0_setup.exe (nécessite NSIS)
```

### Mettre à jour depuis x64dbg officiel

```bat
git remote add upstream https://github.com/x64dbg/x64dbg
git fetch upstream
git merge upstream/development
git submodule update --init --recursive
```

---

## Organisation du dépôt

Fichiers propres à Fathom (le reste est identique à x64dbg) :

```
src/dbg/TraceExport.cpp, .h          commande TraceExport
src/dbg/ftt/ftt.h                    format .ftt (time travel) : écriture et lecture
extracmds/src/ttmodel.*, ttpanel.*   navigation Time Travel, commandes tt.* et onglet
src/dbg/commands/cmd-tracing.cpp     déclaration de la commande
src/tests/trace_export/              test automatique
docs/commands/tracing/TraceExport.md documentation de la commande
pybridge/
  src/                               plugin (serveur, API)
  python/x64dbg_bridge.py            client Python
  examples/                          exemples Python
  headless/                          lanceur et modèles headless
extracmds/src/extracmds.cpp          plugin ExtraCmds (.init, .save, scylla_hide.*, hvkd*, wineye)
extracmds/src/hvkdpanel.h, .cpp      onglet GUI HvKdFusion (Qt, 64 bits)
extracmds/src/emucmds.cpp            commandes emu.* (émulateur)
extracmds/tests/                     tests de Time Travel et de l'émulateur (headless)
triton/                              plugin Triton (sym.*, taint.* : exécution symbolique, teinte)
  src/                               commandes, session sur le débogué ou une trace .ftt
  third_party/Triton/                Triton (Apache-2.0), intégré sans modification (FATHOM.md)
  build.ps1, tests/                  compilation (Z3, Capstone épinglés), test headless
decompiler/                          plugin Decompiler (decompile : le décompilateur de Ghidra sur le débogué)
  third_party/ghidra/                décompilateur, zlib et spécifications sleigh x86 de Ghidra (Apache-2.0, FATHOM.md)
novmp/                               NoVmp + VTIL (dévirtualisation de VMProtect, x64 et x86), vtildump, build.ps1
remill/                              remill (Apache-2.0) compilé pour Windows, build.ps1, dist\ (remill-lift)
binja/                               plugin Binary Ninja (fathom_binja) et son test
revng/                               rev.ng porté sous Windows : third_party/revng (modifié), patches/ (QEMU), build.ps1, dist\
analysis/                            scripts angr / miasm des commandes angr.* et miasm.*, miasm (GPL-2.0+)
extracmds/src/toolcmds.cpp           commandes novmp, remill.lift, revng.decompile, binja, angr.*, miasm.*, analysis.setup
emulator/                            émulateur (dumpulator, Python)
  fathom_emu.py                      ligne de commande des commandes emu.*, écriture des traces x64dbg
  dumpulator/, minidump/             dumpulator (BSL-1.0) et son lecteur de minidumps (MIT), modifications dans README.md
  tests/test_avx.py                  émulation AVX/AVX2 vérifiée contre le processeur
hvkdfusion/                          HvKdFusion (débogage noyau / forensics mémoire Hyper-V, Python)
  hvkdfusion.py, wineye.py, hvkd.ps1 CLI, REPL façon WinDbg, lanceur avec élévation
  plugin_build/                      en-têtes hvlib + hvlib.lib de LiveCloudKd (compilation du plugin hvmm)
  LICENSE                            licence de HvKdFusion et composants tiers
plugins/
  install-plugins.ps1                installeur des plugins tiers
  THIRD-PARTY.md                     versions, licences, empreintes
tools/
  make-release.ps1                   construction des archives de release
  build-installer.ps1                construction de l'installateur NSIS (dist\Fathom_<v>_setup.exe)
  fathom-installer.nsi               script d'installation NSIS
  build-minidump-viewer.ps1          compilation du visualiseur de dumps (src/cross/minidump)
  LISEZMOI.md                        démarrage rapide inclus dans l'archive
README.x64dbg.md                     README officiel d'x64dbg
```

---

## Licences et crédits

- **x64dbg** et **Fathom** : [GPL-3.0](LICENSE). Code source de chaque release : le commit indiqué dans `commithash.txt`.
- **ScyllaHide** : GPL-3.0 — <https://github.com/x64dbg/ScyllaHide> (source de la v1.4 : tag `v1.4`).
- **xAnalyzer** : MIT — <https://github.com/ThunderCls/xAnalyzer>, par ThunderCls.
- **OllyDumpEx** : freeware de Low Priority — <https://low-priority.appspot.com/ollydumpex/>, non redistribué.
- **HvKdFusion** : GPL-3.0-or-later — <https://github.com/EGKrb/HvKdFusion>. Les composants qu'il pilote ne sont pas redistribués : LiveCloudKd (GPL-3.0), MemProcFS et LeechCore (AGPL-3.0), Dokany (LGPL/MIT), ntoseye, WinDbg (licence Microsoft).
- **Plugin Decompiler** : Ghidra, Apache-2.0 — <https://github.com/NationalSecurityAgency/ghidra> (le décompilateur, son zlib et les spécifications x86, intégrés dans `decompiler/third_party/ghidra`).
- **NoVmp** : GPL-3.0 — <https://github.com/can1357/NoVmp>, de Can Bölük ; **VTIL** : BSD-3-Clause — <https://github.com/vtil-project/VTIL-Core> ; liés dans `NoVmp.exe` / `NoVmp32.exe` : Capstone (BSD), Keystone (GPL-2.0), linux-pe (MIT).
- **remill** : Apache-2.0 — <https://github.com/lifting-bits/remill> (Trail of Bits ; intégré dans `remill/third_party/remill`, changements dans son FATHOM.md) ; liés dans `remill-lift-17.exe` : LLVM 17 (Apache-2.0 avec exceptions LLVM), XED (Apache-2.0), glog et gflags (BSD-3-Clause), sleigh (Ghidra, Apache-2.0).
- **rev.ng** : fichiers MIT, GPL-2.0 dans son ensemble — <https://github.com/revng/revng> (rev.ng Labs ; intégré dans `revng/third_party/revng`, changements dans son FATHOM.md) ; avec les forks de rev.ng de QEMU (GPL-2.0, `revng/patches/qemu.patch`), LLVM 16 (Apache-2.0 avec exceptions LLVM) et nanobind (BSD-3-Clause) ; son archive contient Python (PSF) et les paquets qu'utilise rev.ng, glib (LGPL-2.1+), Capstone (BSD) et le runtime MinGW de MSYS2.
- **Binary Ninja** : commercial, de Vector 35 — <https://binary.ninja>, non redistribué ; le plugin `fathom_binja` est celui de Fathom (GPL-3.0-or-later).
- **miasm** : GPL-2.0-or-later — <https://github.com/cea-sec/miasm> (intégré dans `analysis/miasm`) ; **angr** : BSD-2-Clause — <https://github.com/angr/angr> (installé par pip, non redistribué). La méthode de `vm.*` : VMHunt (Xu et al., CCS 2018), réimplémentée.
- **Plugin Triton** : Triton, Apache-2.0 — <https://github.com/JonathanSalwan/Triton>, de Jonathan Salwan et Quarkslab (intégré sans modification dans `triton/third_party/Triton`) ; Z3, MIT — <https://github.com/Z3Prover/z3> (Microsoft ; `libz3.dll` inclus) ; Capstone, BSD-3-Clause — <https://github.com/capstone-engine/capstone> (lié au plugin).
- **dumpulator** : Boost Software License 1.0 — <https://github.com/mrexodia/dumpulator>, de Duncan Ogilvie (mrexodia) ; son lecteur `minidump` : MIT, de Tamas Jos (skelsec). Nécessite Unicorn (GPL-2.0, installé par pip), Capstone (BSD) et pefile (MIT), non inclus.
- **Visualiseur de dumps** : code d'x64dbg (GPL-3.0) ; bibliothèques [udmp-parser](https://github.com/0vercl0k/udmp-parser) (MIT), [cpp-httplib](https://github.com/yhirose/cpp-httplib) (MIT), [linux-pe](https://github.com/can1357/linux-pe) (BSD), [nlohmann/json](https://github.com/nlohmann/json) (MIT), Qt 5.12 (LGPL-3.0, DLL partagées avec x64dbg).

x64dbg est développé par mrexodia et ses contributeurs ([liste](https://github.com/x64dbg/x64dbg/graphs/contributors)) ; voir le [README officiel](README.x64dbg.md) pour les crédits complets. Ce fork n'est ni affilié ni approuvé par l'équipe d'x64dbg ; pour les versions officielles, rendez-vous sur <https://x64dbg.com>.
