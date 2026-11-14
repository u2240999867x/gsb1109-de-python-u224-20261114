```{caution}
This API is not finalised, and may change in a patch version.
```

# `installer.lookup`

Resolve a dotted module name to the wheel artifact that would provide its
file after installation, without installing or importing anything. The wheels
are grouped by the interpreter search root they would land in, and only their
`RECORD` manifests are read.

## Example

```python
from installer.lookup import ModuleResolver, SearchRoot
from installer.sources import WheelFile

root = SearchRoot("site")
with WheelFile.open("demo-1.0.0-py3-none-any.whl") as source:
    root.add_wheel(source)

resolver = ModuleResolver([root])
resolution = resolver.resolve("package.submodule")
resolution.found            # whether the name is importable
resolution.origin           # ModuleFile providing the loaded file
resolution.shadowed         # candidates Python would not use
```

Passing a permutation of search root names re-runs the same resolution with a
different search order, which makes the effect of swapping two roots easy to
compare layer by layer through `ModuleResolution.trace`.

## Reference

```{eval-rst}
.. automodule:: installer.lookup
    :members:
```
