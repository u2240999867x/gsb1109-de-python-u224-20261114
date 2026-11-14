```{caution}
This API is not finalised, and may change in a patch version.
```

# `installer.metadata`

This module separates the lossless structural parse of distribution metadata
from its version-aware interpretation.

- {py:func}`installer.metadata.parse_metadata` retains every field occurrence
  (including duplicates such as `Requires-Dist`), the folded line ranges and
  the body boundary, and can reconstruct the original file.
- {py:func}`installer.metadata.interpret_metadata` explains the parse
  according to the declared `Metadata-Version`, presenting the description
  from the folded `Description` header (older metadata) or the body (newer
  metadata). Header-like lines in the body are never treated as fields, and
  descriptions available from both locations are kept with separate
  provenance.

```{eval-rst}
.. automodule:: installer.metadata
    :members:
```
