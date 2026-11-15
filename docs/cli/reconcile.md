# `python -m installer.reconcile`

This interface compares an existing installation with an offline installation
plan derived from a wheel. It never executes code from the wheel or from the
installation.

Each artifact is classified by provenance:

- `verbatim`: the wheel file must be copied byte-for-byte;
- `rewritten`: only the `#!python` first line may differ, using the selected
  interpreter;
- `generated`: the file comes from an entry point declaration or is the
  installed `RECORD`;
- `unknown`: the file exists in the installation but no wheel member or entry
  point produces it.

`scripts` is scanned recursively for unknown files by default. A file is never
accepted merely because it is in the scripts directory. For every artifact, the
ledger keeps the wheel-source digest, the expected digest, and the installed
digest. An unexpected difference includes the first differing offset and line.

```{argparse}
:module: installer.reconcile
:func: _get_main_parser
:prog: python -m installer.reconcile
```
