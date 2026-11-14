```{caution}
This API is not finalised, and may change in a patch version.
```

# `installer.inventory`

```{eval-rst}
.. automodule:: installer.inventory
```

## Example

```{doctest} pycon
>>> from installer.inventory import Inventory
>>> from installer.records import Hash, RecordEntry
>>> inventory = Inventory()
>>> inventory.import_record("alpha", [RecordEntry("shared.py", Hash("sha256", "aaa"), 3)])
1
>>> inventory.import_record("beta", [RecordEntry("shared.py", Hash("sha256", "bbb"), 3)])
2
>>> inventory.holders("shared.py")
('alpha', 'beta')
>>> sorted(inventory.disputes())
['shared.py']
>>> inventory.remove_distribution("alpha")
3
>>> inventory.holders("shared.py")
('beta',)
>>> inventory.holders("shared.py", generation=2)
('alpha', 'beta')
>>> inventory.rollback(3)
4
>>> inventory.holders("shared.py")
('alpha', 'beta')
```

## Reference

```{eval-rst}
.. autoclass:: installer.inventory.Inventory()
    :members:

.. autoclass:: installer.inventory.AssetStatus()
    :members:

.. autoclass:: installer.inventory.Claim()
    :members:

.. autoexception:: installer.inventory.InventoryError()
```
