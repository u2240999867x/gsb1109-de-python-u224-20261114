import json

import pytest

from radon.cli import Config, diff, trend
from radon.cli.snapshot import SnapshotHarvester, TrendHarvester


def write(path, source):
    path.write_text(source)
    return str(path)


def test_diff_reports_rename_and_move(tmp_path, capsys):
    old = tmp_path / 'old'
    new = tmp_path / 'new'
    old.mkdir()
    new.mkdir()
    write(old / 'core.py', 'def alpha(x):\n    return x + 1\n')
    write(new / 'helpers.py', 'def beta(y):\n    return y + 1\n')

    diff(str(old), str(new))

    output = capsys.readouterr().out
    assert 'alpha' in output
    assert 'beta' in output
    assert 'renamed-moved' in output
    assert 'No rankable complexity changes' not in output


def test_diff_json_contains_relations_and_ranks(tmp_path, capsys):
    old = tmp_path / 'old'
    new = tmp_path / 'new'
    old.mkdir()
    new.mkdir()
    write(old / 'a.py', 'def f(x):\n    return x\n')
    write(
        new / 'a.py',
        'def g(x):\n    if x:\n        return x\n    return -x\n',
    )

    diff(str(old), str(new), rename=['f=g'], json=True)

    payload = json.loads(capsys.readouterr().out)
    assert payload['complexity_changes'][0]['delta'] == 1
    assert payload['complexity_changes'][0]['kind'] == 'renamed'
    assert payload['renames'][0]['status'] == 'grounded'


def test_diff_no_zero_hides_unchanged_deltas(tmp_path, capsys):
    old = tmp_path / 'old'
    new = tmp_path / 'new'
    old.mkdir()
    new.mkdir()
    write(old / 'a.py', 'def f(x):\n    return x\n')
    write(new / 'a.py', 'def g(y):\n    return y\n')

    diff(str(old), str(new), no_zero=True)

    assert 'No rankable complexity changes' in capsys.readouterr().out


def test_diff_marks_ambiguous_wrappers(tmp_path, capsys):
    old = tmp_path / 'old'
    new = tmp_path / 'new'
    old.mkdir()
    new.mkdir()
    write(
        old / 'a.py',
        'def one(x):\n    return x\n\ndef two(x):\n    return x\n',
    )
    write(
        new / 'b.py',
        'def three(x):\n    return x\n\ndef four(x):\n    return x\n',
    )

    diff(str(old), str(new), json=True)

    payload = json.loads(capsys.readouterr().out)
    assert payload['ambiguous']
    assert payload['complexity_changes'] == []


def test_diff_explicit_split(tmp_path, capsys):
    old = tmp_path / 'old'
    new = tmp_path / 'new'
    old.mkdir()
    new.mkdir()
    write(old / 'a.py', 'def orig(x):\n    return x\n')
    write(
        new / 'a.py',
        'def part_one(x):\n    return x\n\n'
        'def part_two(x):\n    return x + 1\n',
    )

    diff(str(old), str(new), split=['orig=part_one,part_two'], json=True)

    payload = json.loads(capsys.readouterr().out)
    assert len(payload['splits']) == 1
    assert len(payload['splits'][0]['sources']) == 1
    assert len(payload['splits'][0]['targets']) == 2


def test_diff_rejects_malformed_mapping(tmp_path, capsys):
    old = tmp_path / 'old'
    new = tmp_path / 'new'
    old.mkdir()
    new.mkdir()
    write(old / 'a.py', 'def f(x):\n    return x\n')
    write(new / 'a.py', 'def g(x):\n    return x\n')

    with pytest.raises(ValueError):
        diff(str(old), str(new), rename=['f:g'])


def test_snapshot_harvester_reports_deterministically(tmp_path):
    old = tmp_path / 'old'
    new = tmp_path / 'new'
    old.mkdir()
    new.mkdir()
    write(old / 'a.py', 'def f(x):\n    return x\n')
    write(
        new / 'a.py',
        'def f(x):\n    if x:\n        return x\n    return -x\n',
    )

    harvester = SnapshotHarvester(str(old), str(new), Config())
    first = harvester.as_json()
    second = harvester.as_json()
    assert first == second


def test_trend_breaks_and_reports_partial_lineages(tmp_path, capsys):
    first = tmp_path / 's1'
    second = tmp_path / 's2'
    third = tmp_path / 's3'
    first.mkdir()
    second.mkdir()
    third.mkdir()
    write(
        first / 'a.py',
        'def process(items):\n'
        '    total = 0\n'
        '    for item in items:\n'
        '        total += item\n'
        '    result = total * 2\n'
        '    return result\n',
    )
    write(
        second / 'a.py',
        'def add_all(items):\n'
        '    total = 0\n'
        '    for item in items:\n'
        '        total += item\n'
        '    return total\n\n'
        'def double(value):\n'
        '    result = value * 2\n'
        '    return result\n',
    )
    write(
        third / 'a.py',
        'def add_all(items):\n'
        '    total = 0\n'
        '    for item in items:\n'
        '        total += item\n'
        '    if total < 0:\n'
        '        total = 0\n'
        '    return total\n\n'
        'def double(value):\n'
        '    result = value * 2\n'
        '    return result\n',
    )

    trend([str(first), str(second), str(third)])

    output = capsys.readouterr().out
    assert 'Partial lineages' in output
    assert 'starts after split' in output
    assert 'Isolated points' in output
    assert 'ends before split' in output


def test_trend_json_keeps_segments_separate(tmp_path, capsys):
    first = tmp_path / 's1'
    second = tmp_path / 's2'
    first.mkdir()
    second.mkdir()
    write(first / 'a.py', 'def one(x):\n    return x\n')
    write(
        second / 'a.py',
        'def one(x):\n    return x\n\ndef two(x):\n    return x\n',
    )

    trend([str(first), str(second)], json=True)

    payload = json.loads(capsys.readouterr().out)
    for item in payload['trends']:
        assert len(item['segments']) == 1 or item['status'] == 'broken'


def test_trend_validates_labels(tmp_path):
    first = tmp_path / 's1'
    first.mkdir()
    write(first / 'a.py', 'def f(x):\n    return x\n')

    with pytest.raises(ValueError):
        trend([str(first)], labels='one,two')
