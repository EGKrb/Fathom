# Contributing

Thanks for your interest in this fork of [x64dbg](https://github.com/x64dbg/x64dbg). Contributions are welcome — bug reports, questions, and pull requests.

## Where things live

This fork tracks the official `development` branch and adds a few self-contained features. The parts that are specific to this fork (and therefore the parts a contribution here usually touches) are listed under **Repository layout** in the [README](README.md): `TraceExport`, the `pybridge` plugin and Python client, the headless templates, the `ExtraCmds` plugin, and the release/packaging tooling under `tools/` and `plugins/`.

Changes to the debugger **core** are generally better sent to [upstream x64dbg](https://github.com/x64dbg/x64dbg) directly, so everyone benefits.

## Reporting a bug or asking a question

- **Bugs**: open an [issue](https://github.com/EGKrb/x64dbg/issues). Please include the fork version (from `commithash.txt` or the release name), your Windows version, whether you run 32- or 64-bit, and the exact commands or Python snippet that reproduce the problem. A trace/log excerpt helps.
- **Questions and ideas**: use [Discussions](https://github.com/EGKrb/x64dbg/discussions).

## Pull requests

1. Fork the repository and create a branch from `main` (e.g. `feature/my-change` or `fix/the-bug`).
2. Keep each PR focused on one feature or fix.
3. If you change `TraceExport`, `pybridge` or `ExtraCmds`, make sure the project still builds and the test suite passes:
   ```bat
   py src\tests\run.py --arch x64
   ```
   Add or update a test under `src/tests/` when you change behavior.
4. Match the surrounding code style. The existing fork code aims to read like the official codebase it sits in.
5. Describe **what** the change does and **why** in the PR body.

## Building

Build instructions (64/32-bit, the plugins, the dump viewer, and the release archives) are in the [README → Building it yourself](README.md#building-it-yourself).

## License

By contributing, you agree that your contribution is licensed under the project's [GPL-3.0](LICENSE) license, consistent with x64dbg itself.

This fork is not affiliated with or endorsed by the official x64dbg team.
