```{caution}
This API is not finalised, and may change in a patch version.
```

# `installer.plan`

`installer.plan` builds an offline installation plan for a wheel, mapping
each archive member to the scheme-relative path it would be installed to.
It detects target paths occupied by more than one member and distinguishes
members with identical contents from content conflicts, using
platform-provided path folding rules.

```{eval-rst}
.. automodule:: installer.plan
    :members:
```
