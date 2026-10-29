# x64dbg-pybridge

Piloter x64dbg (interface graphique ou `headless.exe`) depuis Python, plus des modèles de scripts prêts à l'emploi pour `headless.exe`.

- `src/` : le plugin x64dbg (`pybridge.dp64` / `pybridge.dp32`). Il ouvre un serveur TCP sur **127.0.0.1** et traduit des requêtes JSON en appels à l'API d'x64dbg.
- `python/x64dbg_bridge.py` : le client Python (bibliothèque standard uniquement, rien à installer).
- `examples/` : scripts Python d'exemple.
- `headless/` : lanceur PowerShell et modèles de scripts x64dbg pour `headless.exe`.

> **Sécurité** : le serveur n'accepte que les connexions locales, mais tout programme local qui s'y connecte contrôle complètement le débogueur (lecture/écriture mémoire du processus débogué, exécution de commandes). Ne le démarrez que quand vous en avez besoin.

## Compilation

Prérequis : Visual Studio 2022 (C++), CMake, Ninja. Le SDK de plugins est pris par défaut dans l'installation winget d'x64dbg (`-DX64DBG_SDK=...` pour un autre chemin).

```bat
:: 64 bits (x64dbg)
"C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"
cmake -B build-x64 -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build-x64

:: 32 bits (x32dbg), dans un autre terminal
"C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars32.bat"
cmake -B build-x32 -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build-x32
```

## Installation

Copier le plugin dans le dossier `plugins` d'x64dbg :

```powershell
$x64dbg = "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\x64dbg.x64dbg_Microsoft.Winget.Source_8wekyb3d8bbwe\release"
Copy-Item build-x64\pybridge.dp64 "$x64dbg\x64\plugins\"
Copy-Item build-x32\pybridge.dp32 "$x64dbg\x32\plugins\"
```

Le serveur démarre :

- automatiquement si la variable d'environnement `X64DBG_PYBRIDGE_PORT` est définie (c'est ce que fait `Debugger.launch()`) ;
- sinon avec la commande `pybridge.start [port]` dans x64dbg (port décimal, 27041 par défaut). Aussi : `pybridge.stop`, `pybridge.status`, `pybridge.emu` (voir « Émuler un dump »).

## Utilisation depuis Python

```python
import sys
sys.path.insert(0, r"C:\Users\Elio\Downloads\x64dbg-pybridge\python")
from x64dbg_bridge import Debugger

X64DBG = r"C:\Users\Elio\AppData\Local\Microsoft\WinGet\Packages\x64dbg.x64dbg_Microsoft.Winget.Source_8wekyb3d8bbwe\release"

with Debugger.launch(X64DBG) as dbg:          # démarre headless.exe ; gui=True pour x64dbg.exe
    dbg.init(r"C:\Windows\System32\whoami.exe")
    dbg.set_breakpoint("kernelbase.CreateFileW")
    if dbg.run(wait=True):
        print(dbg.read_string("rcx", wide=True))   # 1er argument (x64)
        print(dbg.regs())
        for ins in dbg.disasm("rip", 5):
            print(hex(ins["address"]), ins["text"])
```

Pour se connecter à un x64dbg déjà ouvert (après `pybridge.start`) : `Debugger()`.

Les adresses et valeurs acceptent un entier ou une **expression x64dbg** : `"kernelbase.CreateFileW"`, `"rsp+8"`, `"[rsp]"`, `"mod.base(target.exe)"`. Dans les expressions, les nombres sont en **hexadécimal** par défaut (`.100` = 100 décimal).

| Catégorie | Méthodes |
|---|---|
| Session | `launch()`, `init(path, arguments, cwd)`, `attach(pid)`, `stop()`, `close()` |
| Exécution | `run(wait, timeout, pass_exceptions)`, `pause()`, `step_into()`, `step_over()`, `step_out()`, `wait(timeout)`, `state()` |
| Registres | `regs()`, `reg(name)`, `set_reg(name, value)` |
| Mémoire | `read(addr, size)`, `write(addr, data)`, `read_ptr(addr)`, `read_string(addr, wide)`, `is_valid(addr)`, `disasm(addr, count)` |
| Breakpoints | `set_breakpoint(addr, condition, log, command, break_, singleshot)`, `delete_breakpoint(addr)`, `set_hardware_breakpoint(addr, kind, size)` |
| Hook d'API | `hook_api(api, log, condition, command, break_)` (log sans pause par défaut), `dump_memory(addr, size, path)` |
| Anti-anti-debug | `hide_debugger()` (patche le PEB du processus débogué), `scyllahide_profiles(ini)`, `scyllahide_set_profile(ini, name)` |
| Traces | `trace(record_file, condition, max_steps, step_over)`, `export_trace(trace, output, fmt)` |
| Commandes | `cmd("texte brut")`, `command(name, *args)` (échappement automatique), `eval(expr)`, `result` (`$result`) |

`export_trace()` utilise la commande `TraceExport`, qui n'existe que dans un x64dbg compilé avec cet ajout (dépôt `x64dbg-git`, branche `feature/trace-export`).

## Exemples

Dans `examples/` (option `--x64dbg <dossier release>` ou variable `X64DBG_DIR` pour choisir la version d'x64dbg) :

| Script | Rôle |
|---|---|
| `api_monitor.py cible.exe` | journalise les appels d'API (CreateFileW, LoadLibraryExW, VirtualProtect...) sans arrêter le programme |
| `trace_to_json.py cible.exe --steps 20000 --out dossier` | enregistre une trace depuis le point d'entrée, l'exporte en JSON/CSV et affiche des statistiques |
| `dump_memory.py --target cible.exe --addr "mod.base(cible.exe)" --size "mod.size(cible.exe)" --out m.bin` | sauvegarde une zone mémoire (ou `--pid` pour s'attacher) |
| `step_log.py cible.exe --count 50` | exécute pas à pas et affiche les registres modifiés par chaque instruction |
| `antidebug.py cible.exe [--scyllahide-ini ...]` | masque le débogueur (PEB) et journalise les sondes anti-debug sans les bloquer |
| `pe_entropy.py cible.exe [--threshold 7.5]` | analyse l'entropie des sections PE du processus en cours, dump celles au-dessus du seuil |
| `memory_diff.py cible.exe --addr ... --size ... --delay 10` | snapshot avant/après d'une plage mémoire, liste les régions modifiées (utile sur packers / décompresseurs runtime) |
| `unpack_dump.py cible.exe --out-dir dumps` | dump auto de chaque région `VirtualProtect`-flipée en PAGE_EXECUTE* (noms horodatés + adresse + taille + flag) |
| `xor_decrypt.py cible.exe --addr ... --size ... --key ... [--brute]` | lit une zone chiffrée depuis le processus et la déchiffre XOR (clé connue ou brute-force 1 octet) |
| `emulate_dump.py crash.dmp --count 200` | **émule** un dump mémoire (pas à pas, trace JSON/CSV, serveur pybridge) — voir ci-dessous |

## Émuler un dump (.dmp)

Un `.dmp` est une **photo figée** de la mémoire : il ne s'exécute pas, il n'a pas de CPU ni de noyau derrière lui. `examples/emulate_dump.py` charge la mémoire capturée et les registres du thread dans l'émulateur **Unicorn** et déroule le code à partir de là. Le parsing du minidump est en bibliothèque standard ; l'émulation demande `pip install unicorn` (et `capstone` en option, pour le désassemblage).

```powershell
py emulate_dump.py crash.dmp --list-threads            # threads + leur rip
py emulate_dump.py crash.dmp --thread 0 --count 200    # pas à pas émulé
py emulate_dump.py crash.dmp --from "rsp+8"            # démarrer ailleurs (expr sur les registres)
py emulate_dump.py crash.dmp --count 5000 --keep-going --trace t.json --trace-csv t.csv
py emulate_dump.py crash.dmp --serve --port 27041      # piloter via x64dbg_bridge.Debugger
```

- **Mode fidèle (défaut)** : s'arrête au **bord du snapshot** — la première page lue que le dump n'a pas capturée, ou le premier appel système. Ce bord est physique. Sur un minidump classique (pile + quelques modules) on l'atteint en quelques instructions ; sur un *full dump* on va très loin.
- **`--keep-going`** : retire cet arrêt en best-effort — pages manquantes mappées à zéro, `syscall`/`int`/`sysenter` sautés, instructions qu'Unicorn ne décode pas sautées. **Dès le premier octet manquant, la trace devient spéculative** (« ce que ferait le CPU si l'absent valait zéro »), pas la vérité du process. Signalé en sortie et par `"skipped": true` dans la trace.
- **`--trace t.json`** : trace émulée en JSON (en-tête + instruction, registres modifiés, accès mémoire). **`--trace-csv t.csv`** : mêmes colonnes que la commande `TraceExport` (`index,thread,address,module,bytes,disasm,<registres>,memory`), donc exploitable avec les mêmes outils.
- **`--serve`** : expose l'émulateur sur le **même protocole que le plugin**, si bien que le client `x64dbg_bridge.Debugger(port=...)` pilote le dump comme une cible vivante (`step_into`, `regs`, `read`, `disasm`...), dans les limites du snapshot.

Depuis x64dbg même, la commande `pybridge.emu "crash.dmp"[, port[, thread[, keepgoing]]]` lance `--serve` dans une console (plugin recompilé requis ; chemin du script via `X64DBG_EMU_SCRIPT`).

## Modèles pour headless.exe

```powershell
.\headless\run-headless.ps1 -Template api-log -Target C:\Windows\System32\whoami.exe
.\headless\run-headless.ps1 -Template trace-export -Target .\programme.exe -X64dbgDir C:\Users\Elio\Downloads\x64dbg-git\bin
```

Le lanceur remplace `%TARGET%`, `%MODULE%`, `%OUT%` et `%BITS%` dans le modèle, lance `headless.exe -cf` avec un dossier de configuration isolé (les `settingset` du modèle ne modifient pas votre configuration d'x64dbg) et écrit tout dans `out\<modèle>-<date>\` (`script.txt`, `headless.log`, fichiers produits). Un modèle se termine par `log "[template] done"`.

| Modèle | Rôle |
|---|---|
| `api-log` | journalise les appels d'API avec leurs arguments, sans pause |
| `trace-export` | trace 50 000 instructions depuis le point d'entrée et exporte en JSON/CSV/Tenet (nécessite `TraceExport`) |
| `dump-module` | sauvegarde le module principal (`.mem`) et un minidump complet au point d'entrée |
| `break-on-file` | breakpoint conditionnel (chemin contenant `.txt`) puis actions automatiques : log, sauvegarde de la pile, minidump |
| `antidebug` | lance la cible avec `hide` (PEB patché : `BeingDebugged`, `NtGlobalFlag`, heap flags) et journalise les sondes anti-debug |
| `dump-at-delay` | laisse la cible s'exécuter puis dump le module principal + minidump (utile sur packers qui déballent au démarrage) |
| `hook-unpack` | log sans pause les API typiques de dépaquetage runtime (`VirtualAlloc`, `VirtualProtect`, `WriteProcessMemory`, `NtMapViewOfSection`, `CryptDecrypt`, `BCryptDecrypt`) |
| `unpack-dump` | log les `VirtualProtect`/`VirtualProtectEx` qui flipent en PAGE_EXECUTE* (mask `0xF0`) et dump la **dernière** région (`vprotect-last.bin`). Pour un dump par appel : `examples/unpack_dump.py` |
| `find-pattern` | `findallmem` sur toute la carte mémoire : pattern hex avec wildcards ou littéral ASCII |
| `run-trace` | enregistre 100 000 instructions depuis le point d'entrée, exporte en JSON et CSV via `TraceExport` |
| `hook-apis-extended` | version élargie de `api-log` (process/thread control, loader, mémoire, I/O, registre, crypto) |
| `emulate-dump` | `-Target` est un `.dmp` : lance `pybridge.emu` et sert l'émulation sur 127.0.0.1:27041 (plugin recompilé requis) |

Les modèles utilisent `arg.get(n)` pour lire les arguments : ils fonctionnent en x64 et en x32 (`-Arch x32`).

## Anti-anti-debug

Deux niveaux, utilisables ensemble :

- **`dbg.hide_debugger()`** (ou commande `hide` dans un script) — masque natif intégré au fork. Patche le PEB du processus débogué dès que vous avez la main : `BeingDebugged = 0`, bits debug (`0x70`) de `NtGlobalFlag` nettoyés, flags de la *process heap* normalisés. Couvre x64 et WoW64. Idempotent, à appeler après `init()` / `attach()` et avant le premier `run()`. Suffit pour les sondes basiques (`IsDebuggerPresent`, `CheckRemoteDebuggerPresent`, lecture directe du PEB).

- **ScyllaHide** — plugin tiers nécessaire pour les sondes plus avancées (`NtQueryInformationProcess`, `NtSetInformationThread`, timing, exceptions...). Déposez ses fichiers (depuis la release ScyllaHide) dans `<x64dbg>\x64\plugins\` et `<x64dbg>\x32\plugins\`, puis sélectionnez le profil adapté à la cible :

    ```python
    ini = r"C:\...\x64dbg\x64\plugins\scylla_hide.ini"
    print(Debugger.scyllahide_profiles(ini))          # ['VMProtect x86/x64', 'Themida x86/x64', ...]
    Debugger.scyllahide_set_profile(ini, "Basic")     # avant de lancer x64dbg
    ```

  Le profil est lu par le plugin au démarrage du processus débogué, donc `scyllahide_set_profile` doit être appelé **avant** `init()` / `attach()`. `Basic` suffit pour la majorité des cas ; les profils packer-spécifiques (VMProtect, Themida, Obsidium, Armadillo) activent en plus les contournements propres à ces protections.

Ces fonctionnalités servent à l'analyse de binaires que vous êtes autorisé à étudier (vos propres programmes, échantillons de malware dans un bac à sable, challenges CTF, etc.). Elles n'affectent que le processus débogué par cette instance d'x64dbg.

## Protocole (pour d'autres langages)

Une requête JSON par ligne, une réponse JSON par ligne :

```
→ {"id": 1, "method": "mem_read", "params": {"addr": "rsp", "size": 16}}
← {"id": 1, "result": "00000000000000009512A857F77F0000"}
← {"id": 2, "error": "cannot read memory"}
```

Méthodes : `ping`, `cmd`, `eval`, `state`, `wait`, `run`, `pause`, `step_into`, `step_over`, `step_out`, `regs`, `reg_get`, `reg_set`, `mem_read`, `mem_write`, `mem_valid`, `disasm` (voir `src/api.cpp`). Les adresses et registres sont renvoyés en chaînes hexadécimales (`"0x7FF6..."`).
