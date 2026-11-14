import json
import os

import pytest

import radon.cli as cli
from radon.cli.harvest import (
    SUPPORTS_IPYNB,
    CCHarvester,
    Harvester,
    MIHarvester,
    RawHarvester,
)
from radon.cli.tools import _is_python_file
from radon.tests.test_cli_harvest import MI_CONFIG, RAW_CONFIG

BASE_CONFIG_WITH_IPYNB = cli.Config(
    exclude="*.py", ignore=None, include_ipynb=True, ipynb_cells=False,
)

BASE_CONFIG_WITH_IPYNB_AND_CELLS = cli.Config(
    exclude="*.py", ignore=None, include_ipynb=True, ipynb_cells=True,
)

DIRNAME = os.path.dirname(__file__)


def cc_config(**overrides):
    values = dict(
        order=lambda block: block.name,
        no_assert=False,
        min='A',
        max='F',
        show_complexity=False,
        show_closures=False,
        average=False,
        total_average=False,
    )
    values.update(BASE_CONFIG_WITH_IPYNB_AND_CELLS.config_values)
    values.update(overrides)
    return cli.Config(**values)


def write_notebook(tmpdir, cells, name='cells.ipynb'):
    '''Write a notebook with the given ``(cell_id, source, execution_count)``
    tuples and return its path.
    '''
    notebook = {
        'cells': [
            {
                'cell_type': 'code',
                'id': cell_id,
                'execution_count': execution_count,
                'metadata': {},
                'outputs': [],
                'source': source.splitlines(True),
            }
            for cell_id, source, execution_count in cells
        ],
        'metadata': {},
        'nbformat': 4,
        'nbformat_minor': 5,
    }
    path = os.path.join(str(tmpdir), name)
    with open(path, 'w') as fobj:
        json.dump(notebook, fobj)
    return path


@pytest.mark.skipif(not SUPPORTS_IPYNB, reason="nbformat not installed")
def test_harvestor_yields_ipynb(log_mock):
    '''Test that Harvester will try ipynb files when configured'''
    target = os.path.join(DIRNAME, 'data', 'example.ipynb')
    harvester = Harvester([DIRNAME], BASE_CONFIG_WITH_IPYNB)
    filenames = list(harvester._iter_filenames())
    assert _is_python_file(target)
    assert len(filenames) == 1
    assert target in filenames


@pytest.mark.skipif(not SUPPORTS_IPYNB, reason="nbformat not installed")
def test_ipynb(log_mock):
    mi_cfg = cli.Config(**BASE_CONFIG_WITH_IPYNB.config_values)
    mi_cfg.config_values.update(MI_CONFIG.config_values)
    raw_cfg = cli.Config(**BASE_CONFIG_WITH_IPYNB.config_values)
    raw_cfg.config_values.update(RAW_CONFIG.config_values)
    cc_cfg = cli.Config(
        order=lambda block: block.name,
        no_assert=False,
        min='A',
        max='F',
        show_complexity=False,
        show_closures=False,
        average=True,
        total_average=False,
    )
    cc_cfg.config_values.update(BASE_CONFIG_WITH_IPYNB.config_values)

    mappings = {
        MIHarvester: mi_cfg,
        RawHarvester: raw_cfg,
        CCHarvester: cc_cfg,
    }
    target = 'data/'
    fnames = [
        os.path.join(DIRNAME, target),
    ]
    for h_class, cfg in mappings.items():
        for f in fnames:
            harvester = h_class([f], cfg)
            out = harvester.as_json()
            assert not any(['error' in out]), out


@pytest.mark.skipif(not SUPPORTS_IPYNB, reason="nbformat not installed")
def test_ipynb_with_cells(mocker, log_mock):
    mi_cfg = cli.Config(**BASE_CONFIG_WITH_IPYNB_AND_CELLS.config_values)
    mi_cfg.config_values.update(MI_CONFIG.config_values)
    raw_cfg = cli.Config(**BASE_CONFIG_WITH_IPYNB_AND_CELLS.config_values)
    raw_cfg.config_values.update(RAW_CONFIG.config_values)
    cc_cfg = cli.Config(
        order=lambda block: block.name,
        no_assert=False,
        min='A',
        max='F',
        show_complexity=False,
        show_closures=False,
        average=True,
        total_average=False,
    )
    cc_cfg.config_values.update(BASE_CONFIG_WITH_IPYNB_AND_CELLS.config_values)

    mappings = {
        MIHarvester: mi_cfg,
        RawHarvester: raw_cfg,
        CCHarvester: cc_cfg,
    }
    target = 'data/'
    fnames = [
        os.path.join(DIRNAME, target),
    ]
    for h_class, cfg in mappings.items():
        for f in fnames:
            harvester = h_class([f], cfg)
            out = harvester.as_json()
            assert not any(['error' in out]), out


@pytest.mark.skipif(not SUPPORTS_IPYNB, reason="nbformat not installed")
def test_raw_ipynb(log_mock):
    raw_cfg = cli.Config(**BASE_CONFIG_WITH_IPYNB.config_values)

    target = os.path.join(DIRNAME, 'data', 'example.ipynb')
    harvester = RawHarvester([DIRNAME], raw_cfg)
    out = json.loads(harvester.as_json())
    assert harvester.config.include_ipynb == True
    assert target in out
    assert out[target]['loc'] == 63
    assert out[target]['lloc'] == 37
    assert out[target]['sloc'] == 37
    assert out[target]['comments'] == 3
    assert out[target]['multi'] == 10
    assert out[target]['blank'] == 14
    assert out[target]['single_comments'] == 2


@pytest.mark.skipif(not SUPPORTS_IPYNB, reason="nbformat not installed")
def test_raw_ipynb_cells(log_mock):
    raw_cfg = cli.Config(**BASE_CONFIG_WITH_IPYNB_AND_CELLS.config_values)

    target = os.path.join(DIRNAME, 'data', 'example.ipynb')
    harvester = RawHarvester([DIRNAME], raw_cfg)
    out = json.loads(harvester.as_json())
    cell_target = target + ":[3]"
    assert target in out
    assert cell_target in out
    assert out[cell_target]['loc'] == 52
    assert out[cell_target]['lloc'] == 27
    assert out[cell_target]['sloc'] == 27
    assert out[cell_target]['comments'] == 3
    assert out[cell_target]['multi'] == 10
    assert out[cell_target]['blank'] == 13
    assert out[cell_target]['single_comments'] == 2


@pytest.mark.skipif(not SUPPORTS_IPYNB, reason="nbformat not installed")
def test_cc_ipynb_reports_original_cell_lines(tmpdir, log_mock):
    '''Blocks are reported in the original cell coordinates, not in those of
    the joined and magic-stripped document.
    '''
    target = write_notebook(
        tmpdir,
        [
            ('first', '%matplotlib inline\ndef f(x):\n    return x', 7),
            ('second', 'def g(y):\n    return y\n', 2),
        ],
    )
    harvester = CCHarvester([target], cc_config())
    out = json.loads(harvester.as_json())
    blocks = {b['name']: b for b in out[target]}

    assert blocks['f'] == {
        'type': 'function',
        'name': 'f',
        'rank': 'A',
        'complexity': 1,
        'col_offset': 0,
        'lineno': 2,
        'endline': 3,
        'closures': [],
        'cell': 'first',
        'cell_id': 'first',
        'cell_lineno': 2,
        'cell_endline': 3,
        'execution_count': 7,
    }
    assert blocks['g']['cell'] == 'second'
    assert blocks['g']['lineno'] == 4
    assert (blocks['g']['cell_lineno'], blocks['g']['cell_endline']) == (1, 2)


@pytest.mark.skipif(not SUPPORTS_IPYNB, reason="nbformat not installed")
def test_cc_ipynb_cell_keys_use_cell_ids(tmpdir, log_mock):
    '''Per-cell reports are keyed by cell identity, whatever the order in
    which the cells were executed.
    '''
    target = write_notebook(
        tmpdir,
        [
            ('first', '%matplotlib inline\ndef f(x):\n    return x', 7),
            ('second', 'def g(y):\n    return y\n', 2),
        ],
    )
    harvester = CCHarvester([target], cc_config(ipynb_cells=True))
    out = json.loads(harvester.as_json())

    assert sorted(out) == [
        target,
        '{0}:[first]'.format(target),
        '{0}:[second]'.format(target),
    ]
    assert [b['name'] for b in out[target]] == ['f', 'g']


@pytest.mark.skipif(not SUPPORTS_IPYNB, reason="nbformat not installed")
def test_cc_ipynb_cells_follow_stable_ids_when_reordered(tmpdir, log_mock):
    '''Reordering cells must not reattach a function to a different cell.'''
    cells = [
        ('alpha', 'def f(x):\n    return x', 3),
        ('beta', 'def g(y):\n    return y', 8),
    ]
    first = write_notebook(tmpdir, cells, name='first.ipynb')
    swapped = write_notebook(tmpdir, list(reversed(cells)), name='b.ipynb')
    cfg = cc_config(ipynb_cells=True, order=lambda block: block.lineno)

    out_a = json.loads(CCHarvester([first], cfg).as_json())
    out_b = json.loads(CCHarvester([swapped], cfg).as_json())

    for key in ('alpha', 'beta'):
        assert out_a['{0}:[{1}]'.format(first, key)] == out_b[
            '{0}:[{1}]'.format(swapped, key)
        ]

    document_a = out_a[first]
    document_b = out_b[swapped]
    summary_a = [
        (b['name'], b['cell'], b['execution_count']) for b in document_a
    ]
    summary_b = [
        (b['name'], b['cell'], b['execution_count']) for b in document_b
    ]
    # Document order wins over execution counts.
    assert summary_a == [('f', 'alpha', 3), ('g', 'beta', 8)]
    assert summary_b == [('g', 'beta', 8), ('f', 'alpha', 3)]
    # Line numbers follow the cell, not the position in the document.
    assert [(b['lineno'], b['cell_lineno']) for b in document_a] == [
        (1, 1),
        (3, 1),
    ]
    assert [(b['lineno'], b['cell_lineno']) for b in document_b] == [
        (1, 1),
        (3, 1),
    ]


@pytest.mark.skipif(not SUPPORTS_IPYNB, reason="nbformat not installed")
def test_cc_ipynb_duplicate_sources_without_ids(tmpdir, log_mock):
    '''Cells without IDs but with the same source are distinct occurrences.'''
    notebook = {
        'cells': [
            {
                'cell_type': 'code',
                'execution_count': 1,
                'metadata': {},
                'outputs': [],
                'source': 'def h(z):\n    return z',
            },
            {
                'cell_type': 'code',
                'execution_count': 2,
                'metadata': {},
                'outputs': [],
                'source': 'def h(z):\n    return z',
            },
        ],
        'metadata': {},
        'nbformat': 4,
        'nbformat_minor': 0,
    }
    target = os.path.join(str(tmpdir), 'duplicates.ipynb')
    with open(target, 'w') as fobj:
        json.dump(notebook, fobj)

    cfg = cc_config(ipynb_cells=True)
    out = json.loads(CCHarvester([target], cfg).as_json())

    assert '{0}:[0]'.format(target) in out
    assert '{0}:[1]'.format(target) in out
    assert out['{0}:[0]'.format(target)][0]['execution_count'] == 1
    assert out['{0}:[1]'.format(target)][0]['execution_count'] == 2
    assert len(out[target]) == 2


@pytest.mark.skipif(not SUPPORTS_IPYNB, reason="nbformat not installed")
def test_cc_ipynb_ignores_empty_cells_and_repeated_names(tmpdir, log_mock):
    '''Empty cells produce no blocks, and repeated names stay distinct.'''
    target = write_notebook(
        tmpdir,
        [
            ('one', 'def f():\n    return 1', 4),
            ('empty', '', None),
            ('blank', '\n\n', None),
            ('two', '%time\ndef f():\n    return 2', 9),
        ],
    )
    harvester = CCHarvester([target], cc_config())
    out = json.loads(harvester.as_json())

    assert [b['name'] for b in out[target]] == ['f', 'f']
    assert [b['cell'] for b in out[target]] == ['one', 'two']
    assert out[target][0]['lineno'] == 1
    assert out[target][1]['lineno'] == 8

