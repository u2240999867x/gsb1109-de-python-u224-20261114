```{caution}
This API is not finalised, and may change in a patch version.
```

# `installer.records`

```{eval-rst}
.. automodule:: installer.records
```

## Example

```{doctest} pycon
>>> from installer.records import parse_record_file, RecordEntry
>>> lines = [
...     "file.py,sha256=AVTFPZpEKzuHr7OvQZmhaU3LvwKz06AJw8mT\\_pNh2yI,3144",
...     "distribution-1.0.dist-info/RECORD,,",
... ]
>>> records = parse_record_file(lines)
>>> li = list(records)
>>> len(li)
2
>>> record_tuple = li[0]
>>> record_tuple
('file.py', 'sha256=AVTFPZpEKzuHr7OvQZmhaU3LvwKz06AJw8mT\\_pNh2yI', '3144')
>>> record = RecordEntry.from_elements(*record_tuple)
>>> record
RecordEntry(path='file.py', hash_=Hash(name='sha256', value='AVTFPZpEKzuHr7OvQZmhaU3LvwKz06AJw8mT\\_pNh2yI'), size=3144)
>>> record.path
'file.py'
>>> record.hash_
Hash(name='sha256', value='AVTFPZpEKzuHr7OvQZmhaU3LvwKz06AJw8mT\\_pNh2yI')
>>> record.size
3144
>>> record.validate(b"...")
False
```

## Structured validation

A blank hash or size cell is a missing declaration, not a declared zero. Use
`RecordRow.size_declared`, `RecordEntry.size_declared`, or the status of a
validation result to distinguish an unstated size from an empty file:

```{doctest} pycon
>>> from io import BytesIO
>>> from installer.records import RecordStatus, parse_record_rows
>>> blank = parse_record_rows(["empty.bin,,"]).__next__()
>>> blank.size_declared
False
>>> RecordEntry.from_elements(*blank.elements).validation_result(b"").status
<RecordStatus.ABSENT: 'absent'>
>>> zero = parse_record_rows(
...     ["empty.bin,sha256=47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU,0"]
... ).__next__()
>>> RecordEntry.from_elements(*zero.elements).validation_result(b"").status
<RecordStatus.VERIFIED: 'verified'>
```

`RecordValidationResult` reports the overall `status`, per-field
`hash_status` / `size_status`, the original `raw_path`, and `occurrences`
for every row that declared the path. A zero-byte file whose hash does not
match is still a `RecordStatus.MISMATCH`.

Duplicate declarations are grouped rather than collapsed last-wins:

```{doctest} pycon
>>> from installer.records import group_record_rows
>>> rows = parse_record_rows([
...     "file.bin,sha256=ABC,0",
...     "file.bin,sha256=ABC,0",
... ])
>>> group = group_record_rows(rows)["file.bin"]
>>> len(group.occurrences), group.conflicting
(2, False)
```

If the declarations differ, `RecordGroup.conflicting` is true and
`RecordGroup.require_entry()` raises `ConflictingRecordEntry` instead of
silently choosing a row.

## Reference

```{eval-rst}
.. autofunction:: installer.records.parse_record_file

.. autofunction:: installer.records.parse_record_rows

.. autofunction:: installer.records.group_record_rows

.. autoclass:: installer.records.RecordRow()
    :members:

.. autoclass:: installer.records.RecordEntry()
    :special-members: __init__
    :members:

.. autoclass:: installer.records.RecordStatus()
    :members:

.. autoclass:: installer.records.RecordValidationResult()
    :members:

.. autoclass:: installer.records.Hash()
    :special-members: __init__
    :members:
```
