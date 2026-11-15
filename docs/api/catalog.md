```{caution}
This API is not finalised, and may change in a patch version.
```

# `installer.catalog`

Provides a release-scoped, searchable catalog of entry point declarations,
built from any number of ``entry_points.txt`` files.

Every declaration is stored together with the release that declared it
(distribution identity and version), its entry point group, its command name,
and its conditions (requested extras and a :pep:`508` environment marker).

```{doctest} pycon
>>> from installer.catalog import CommandCatalog
>>> catalog = CommandCatalog()
>>> catalog.add_release("demo", "1.0", """
... [console_scripts]
... demo = demo.cli:main
... legacy = demo.legacy:main
... """)
>>> authors = catalog.add_release("demo", "2.0", """
... [console_scripts]
... demo = demo.cli:main
... """)
```

Lookups only consider the releases selected by the caller. An entry point that
only existed in an older release is not reported for a release that does not
declare it:

```{doctest} pycon
>>> [provider.name for provider in catalog.find_providers("legacy")]
['legacy']
>>> catalog.find_providers("legacy", releases=["demo==2.0"])
()
```

GUI and console declarations with the same name are distinct providers, while
two providers in the same group make the lookup ambiguous:

```{doctest} pycon
>>> catalog.add_release("demo", "2.0", """
... [console_scripts]
... demo = demo.cli:main
...
... [gui_scripts]
... demo = demo.gui:main
... """)
>>> result = catalog.query("demo", releases=["demo==2.0"])
>>> sorted(result.groups)
['console_scripts', 'gui_scripts']
>>> result.ambiguous
False
>>> result.unique("console").entry_point
'demo.cli:main'
```

When two console providers match, callers can inspect the candidates through
{any}`QueryResult` or let {any}`CommandCatalog.resolve` raise
{any}`AmbiguousCommand`:

```{doctest} pycon
>>> from installer.catalog import AmbiguousCommand
>>> catalog.add_release("other", "1.0", "[console_scripts]\ndemo = other:main\n")
>>> catalog.resolve("demo", releases=["demo==2.0", "other==1.0"])
Traceback (most recent call last):
    ...
installer.catalog.AmbiguousCommand: Ambiguous command 'demo' in group 'console_scripts': demo==2.0 [console_scripts] demo = demo.cli:main, other==1.0 [console_scripts] demo = other:main
```

Conditions are evaluated against a :pep:`508` environment, which defaults to
the running interpreter:

```{doctest} pycon
>>> catalog.add_release("demo", "3.0", """
... [console_scripts]
... modern = demo:modern ; python_version >= "3.12"
... dev = demo:dev [development]
... """)
>>> catalog.find_providers("modern", environment={"python_version": "3.10"})
()
>>> catalog.find_providers("modern", environment={"python_version": "3.12"})[0].version
'3.0'
>>> catalog.find_providers("dev", extras=["development"])[0].name
'dev'
```

## Reference

```{eval-rst}
.. autoclass:: installer.catalog.CommandCatalog()
    :special-members: __init__
    :members:

.. autoclass:: installer.catalog.CommandProvider()
    :special-members: __init__
    :members:

.. autoclass:: installer.catalog.Release()

.. autoclass:: installer.catalog.QueryResult()
    :members:

.. autoclass:: installer.catalog.AmbiguousCommand()
.. autoclass:: installer.catalog.CommandNotFound()
.. autoclass:: installer.catalog.InvalidCommand()

.. autofunction:: installer.catalog.parse_entry_points
.. autofunction:: installer.catalog.parse_entry_points_file
.. autofunction:: installer.catalog.evaluate_marker
.. autofunction:: installer.catalog.normalize_group
.. autofunction:: installer.catalog.default_environment
.. autofunction:: installer.catalog.build_catalog
```
