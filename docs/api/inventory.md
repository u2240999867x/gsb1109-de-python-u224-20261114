```{caution}
This API is not finalised, and may change in a patch version.
```

# `installer.inventory`

```{eval-rst}
.. automodule:: installer.inventory
    :members:
```

## Example

```python
from installer.inventory import Inventory

inventory = Inventory.from_wheels([
    "demo-1.0-cp310-cp310-manylinux_x86_64.whl",
    "demo-1.0-cp310-cp310-win_amd64.whl",
])
view = inventory.get("demo", "1.0")

view.project_metadata["Summary"]  # metadata all artifacts agree on
view.divergences["Requires-Python"].groups  # values plus declaring artifacts
view.divergence_sources("Requires-Python")  # value -> declaring filenames
view.platform_metadata  # per-wheel tags and WHEEL metadata
view.dependencies  # per-wheel Requires-Dist conditions
view.files  # per-wheel RECORD entries
```
