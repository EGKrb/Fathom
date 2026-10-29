> 🇬🇧 **This README is also available in [English](README.md).**

# Fathom

<img width="100" src="src/fathom-logo.png" alt="Fathom"/>

[![Licence : GPL-3.0](https://img.shields.io/badge/License-GPL--3.0-blue.svg)](LICENSE) [![Plateforme : Windows](https://img.shields.io/badge/Plateforme-Windows%20x86%2Fx64-0078D6.svg)](#) [![Release](https://img.shields.io/badge/release-v1.0.0-success.svg)](https://github.com/EGKrb/Fathom/releases/latest) [![Basé sur x64dbg](https://img.shields.io/badge/bas%C3%A9%20sur-x64dbg-444.svg)](https://github.com/x64dbg/x64dbg)

**Fathom** — une boîte à outils Windows de rétro-ingénierie et de forensic mémoire, de l'instruction isolée à la machine entière. Elle est **construite sur [x64dbg](https://github.com/x64dbg/x64dbg)** (GPL-3.0), le débogueur open source pour Windows (analyse de malwares, rétro-ingénierie d'exécutables sans code source), dont elle reprend la branche `development` officielle, et ajoute :

- **`TraceExport`** : conversion des traces d'exécution en **JSON** ou **CSV** lisibles par n'importe quel outil, ou en traces texte **Tenet** / **Frinet** pour les explorer dans IDA Pro ;
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
5. [pybridge : piloter x64dbg depuis Python](#pybridge--piloter-x64dbg-depuis-python)
6. [Modèles pour headless.exe](#modèles-pour-headlessexe)
7. [Commandes ajoutées et dumps mémoire](#commandes-ajoutées-et-dumps-mémoire)
8. [HvKdFusion : débogage noyau et forensics mémoire Hyper-V](#hvkdfusion--débogage-noyau-et-forensics-mémoire-hyper-v)
9. [Plugins inclus](#plugins-inclus)
10. [Compiler soi-même](#compiler-soi-même)
11. [Organisation du dépôt](#organisation-du-dépôt)
12. [Licences et crédits](#licences-et-crédits)

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
  x64\  x32\                 débogueur 64 / 32 bits, headless.exe, plugins\
  x64\minidump.exe           visualiseur de dumps mémoire (.dmp)
  translations\              interface traduite (dont le français : Options > Langue)
  themes\                    thème sombre
  pybridge\                  client Python, exemples, modèles headless
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
| Export des traces | binaire `.trace64` lisible uniquement par x64dbg ; export CSV possible seulement dans l'interface | commande `TraceExport` → JSON, CSV, Tenet ou Frinet, aussi en script et dans `headless.exe` |
| Automatisation | langage de script x64dbg, SDK C/C++ | + client Python (pybridge) + modèles headless avec lanceur |
| Dumps mémoire (`.dmp`) | création seulement (`minidump`) | + visualiseur de dumps (`minidump.exe`), ouvert par `.init fichier.dmp` ; les dumps noyau et images mémoire brutes s'ouvrent dans wineye |
| Noyau / machine entière | — (mode utilisateur seulement) | HvKdFusion : lecture/écriture/dump de la mémoire d'une VM Hyper-V, WinDbg/kd sur une VM, montage MemProcFS, REPL wineye (`hvkd`, `wineye`) |
| Commandes | — | `.init`, `.save`, `scylla_hide.enable` / `.disable` / `.status`, `hvkd`, `hvkd.log`, `hvkd.check`, `wineye` (plugin ExtraCmds) |
| Plugins | aucun | ScyllaHide et xAnalyzer inclus, pybridge et ExtraCmds inclus, OllyDumpEx via `install-plugins.ps1` |
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
TraceExport fichier_trace, fichier_sortie[, format[, slide]]
```

| Argument | Description |
|---|---|
| `fichier_trace` | trace à lire : `.trace64` avec x64dbg, `.trace32` avec x32dbg |
| `fichier_sortie` | fichier créé (écrasé s'il existe) |
| `format` | `json`, `csv`, `tenet` ou `frinet`. Sans ce 3e argument : `csv` si le nom se termine par `.csv`, `frinet` s'il se termine par `.tenet`, sinon `json` |
| `slide` | `frinet` uniquement, facultatif : décalage ASLR (base du module tracé à l'exécution, par ex. `mod.main()`). Par défaut : base du module débogué si la trace lui appartient, sinon aucun |

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

Limites : x64dbg enregistre chaque accès mémoire comme une valeur de la taille d'un pointeur, un accès peut donc couvrir plus d'octets que l'opérande ; une trace multi-thread est écrite comme une seule chronologie ; en `tenet`, la mémoire de la dernière instruction est perdue (pas de ligne suivante), et en `frinet` les registres après la dernière instruction (ou avant un changement de thread) sont inconnus.

### Fonctionnement interne

[`src/dbg/TraceExport.cpp`](src/dbg/TraceExport.cpp) lit le fichier en continu (mémoire constante, quelle que soit la taille de la trace) :

1. vérifie l'en-tête `TRAC` et son JSON (version 1, architecture identique à celle du débogueur) ;
2. pour chaque bloc d'instruction, applique les registres modifiés à l'état courant (les registres sont encodés par différence), lit les accès mémoire, puis désassemble les octets ;
3. ignore les blocs utilisateur (type ≥ 0x80) prévus par le format ;
4. écrit le JSON, le CSV, la trace Tenet ou Frinet au fil de l'eau (Tenet/Frinet gardent l'instruction précédente pour placer les accès mémoire et les registres après exécution sur la bonne ligne).

Sécurités : un fichier de sortie identique au fichier d'entrée est refusé (il serait écrasé) ; une trace **en cours d'enregistrement** peut être exportée, son dernier bloc incomplet est ignoré avec un avertissement ; un fichier corrompu donne un message précis indiquant après combien d'instructions.

Limites : seuls les registres généraux, le pointeur d'instruction et les drapeaux sont exportés (pas les registres SSE/AVX/x87, pourtant présents dans la trace) ; une trace 32 bits doit être exportée avec x32dbg et inversement.

Documentation de la commande : [`docs/commands/tracing/TraceExport.md`](docs/commands/tracing/TraceExport.md). Test automatique : [`src/tests/trace_export/`](src/tests/trace_export).

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
| | `export_trace(trace, sortie, fmt, slide)` | `TraceExport` (`fmt` : `json`, `csv`, `tenet`, `frinet`) ; renvoie le nombre d'instructions |
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
:: Plugins pybridge et ExtraCmds (même terminal que l'architecture voulue ; build-x32 en 32 bits)
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
.\tools\make-release.ps1 -Version 1.0.0              # archives de release dans dist\ (stage aussi release\)
.\tools\build-installer.ps1 -Version 1.0.0           # dist\Fathom_1.0.0_setup.exe (nécessite NSIS)
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
- **Visualiseur de dumps** : code d'x64dbg (GPL-3.0) ; bibliothèques [udmp-parser](https://github.com/0vercl0k/udmp-parser) (MIT), [cpp-httplib](https://github.com/yhirose/cpp-httplib) (MIT), [linux-pe](https://github.com/can1357/linux-pe) (BSD), [nlohmann/json](https://github.com/nlohmann/json) (MIT), Qt 5.12 (LGPL-3.0, DLL partagées avec x64dbg).

x64dbg est développé par mrexodia et ses contributeurs ([liste](https://github.com/x64dbg/x64dbg/graphs/contributors)) ; voir le [README officiel](README.x64dbg.md) pour les crédits complets. Ce fork n'est ni affilié ni approuvé par l'équipe d'x64dbg ; pour les versions officielles, rendez-vous sur <https://x64dbg.com>.
