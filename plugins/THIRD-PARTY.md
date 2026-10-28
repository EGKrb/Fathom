# Plugins tiers

Ces plugins ne sont pas développés dans ce dépôt. `install-plugins.ps1` les télécharge depuis leur source officielle et vérifie leur empreinte SHA-256.

| Plugin / Outil | Version | Auteur | Licence | Source | Dans la release |
|---|---|---|---|---|---|
| ScyllaHide | v1.4 (2023-03-24) | x64dbg / NtQuery | GPL-3.0 | <https://github.com/x64dbg/ScyllaHide> (code source de cette version : <https://github.com/x64dbg/ScyllaHide/tree/v1.4>) | oui |
| xAnalyzer | 2.5.12 (2025-08-13) | ThunderCls | MIT | <https://github.com/ThunderCls/xAnalyzer> | oui |
| OllyDumpEx | v1.86 (2024-10-04) | Low Priority | aucune licence publiée (freeware, code fermé) | <https://low-priority.appspot.com/ollydumpex/> | **non** : sans licence de redistribution, il est téléchargé chez l'utilisateur par `install-plugins.ps1` |
| TitanHide | v0019 (2025-06-01) | mrexodia et contributeurs | BSD-3-Clause | <https://github.com/mrexodia/TitanHide> | **non** : Windows Defender et d'autres AV flaggent systématiquement le driver (`TitanHide.sys`) comme HackTool. `install-plugins.ps1` peut le télécharger à la demande une fois une exclusion AV ajoutée (voir `README-TitanHide.md`). Nécessite aussi test-signing + bypass PatchGuard. |
| PE-sieve | v0.4.1.1 (2025-09-13) | hasherezade | BSD-2-Clause | <https://github.com/hasherezade/pe-sieve> | oui — scanner d'anomalies PE in-memory, deux .exe standalone sous `tools\pesieve\`. |

Les textes des licences sont dans le dossier `licenses/` de la release.

## Composants pilotés par HvKdFusion (`hvkdfusion/`)

HvKdFusion (GPL-3.0-or-later, <https://github.com/EGKrb/HvKdFusion>) est livré dans la release, mais **aucun** des composants qu'il pilote : l'utilisateur les installe ou les compile, puis `hvkdfusion.py setup` les localise.

| Composant | Licence | Source | Dans la release |
|---|---|---|---|
| LiveCloudKd (`hvlib.dll`, `hvmm.sys`, `LiveCloudKd.exe`) | GPL-3.0 | <https://github.com/gerhart01/LiveCloudKd> | **non** : contient un pilote noyau |
| MemProcFS (`vmm.dll`, `MemProcFS.exe`) | AGPL-3.0 | <https://github.com/ufrisk/MemProcFS> | **non** |
| LeechCore (`leechcore.dll`, plugin `hvmm`) | AGPL-3.0 | <https://github.com/ufrisk/LeechCore> | **non** |
| Dokany (`dokan2.sys`) | LGPL-3.0 / MIT | <https://github.com/dokan-dev/dokany> | **non** : pilote noyau signé, à installer avec `DokanSetup.exe` |
| ntoseye | sa propre licence | <https://github.com/dmaivel/ntoseye> | **non** : tourne sous WSL |
| WinDbg / kd | licence Microsoft | <https://learn.microsoft.com/windows-hardware/drivers/debugger/> | **non** : non redistribuable |

## Bibliothèques du visualiseur de dumps (`x64\minidump.exe`)

Compilées dans le visualiseur par `tools/build-minidump-viewer.ps1` (versions de `src/cross/vendor/cmake.toml`) :

| Bibliothèque | Version | Licence |
|---|---|---|
| [udmp-parser](https://github.com/0vercl0k/udmp-parser) | `2fff7ac` | MIT |
| [cpp-httplib](https://github.com/yhirose/cpp-httplib) | v0.25.0 | MIT |
| [linux-pe](https://github.com/can1357/linux-pe) | `1fcb057` | BSD-2-Clause |
| [nlohmann/json](https://github.com/nlohmann/json) | v3.12.0 | MIT |

## Empreintes SHA-256 des fichiers téléchargés

| Fichier | SHA-256 |
|---|---|
| `ScyllaHide_2023-03-24_13-03.zip` | `edeb0dd203fd1ef38e1404e8a1bd001e05c50b6096e49533f546d13ffdcb7404` |
| `xAnalyzer.dp32` | `ec586fdc19e87656a630c8b4359495e0a5dc4d29bf0bb0781c4ae907efbe8a08` |
| `xAnalyzer.dp64` | `8240a3ca76b21f4181fef0b047521177a5cbff13b9760340a35848b782b38117` |
| `apis_def.zip` (xAnalyzer) | `606c9b3144de24878817fcf8391e399ce00c67651abe85373358db9195aa47f1` |
| `OllyDumpEx.zip` | `3b39e7d8d0b8d1c1407ec93531f2a35fd57ee7d26d4ce71cdc1dc3d9d766f758` |
| `TitanHide-f1fbb18ec89ce8d072296faeda8ff7aa9a4468cb.zip` | `6799ea6f77ef190dab04ff03923242446ce0bcebfed98d7872e99ba43d2fe2b6` |
| `pe-sieve32.exe` | `ee684b34b37af24d1c1e9ca80ceeee6878cc80bb24c0c3793840c9acf905bd59` |
| `pe-sieve64.exe` | `9f3ff2884a2c61006cd0a92b7572a815b8dc17012be7747a6abd6ca07c503a3b` |
