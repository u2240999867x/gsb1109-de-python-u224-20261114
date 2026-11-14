'''This module holds the base Harvester class and all its subclassess.'''

import collections
import json
import sys
from builtins import super

from radon.cli.colors import MI_RANKS, RANKS_COLORS, RESET
from radon.cli.tools import (
    _open,
    cc_to_dict,
    cc_to_terminal,
    dict_to_codeclimate_issues,
    dict_to_xml,
    dict_to_md,
    iter_filenames,
    raw_to_dict,
    strip_ipython_with_map,
)
from radon.complexity import (
    add_inner_blocks,
    cc_rank,
    cc_visit,
    sorted_results,
)
from radon.metrics import h_visit, mi_rank, mi_visit
from radon.raw import analyze

if sys.version_info[0] < 3:
    from StringIO import StringIO
else:
    from io import StringIO

try:
    import nbformat

    SUPPORTS_IPYNB = True
except ImportError:
    SUPPORTS_IPYNB = False


_NotebookCell = collections.namedtuple(
    '_NotebookCell',
    [
        'index',
        'ordinal',
        'cell_id',
        'execution_count',
        'source',
        'start_line',
        'line_count',
    ],
)


class _NotebookSource(object):
    '''Line mapping for a piece of code extracted from a notebook.

    ``code`` is the source that is actually analyzed: ``%`` lines have been
    removed. ``line_map`` maps every line of ``code`` back to its line number
    in the original notebook (or cell, when only one cell is analyzed).
    Mapping back is what makes it possible to return the original cell and its
    line range instead of the line numbers of the joined/stripped source.
    '''

    def __init__(self, code, cells, line_map):
        self.code = code
        self.cells = cells
        self.line_map = line_map

    def original_line(self, lineno):
        '''Return the original line number for an analyzed line.'''
        try:
            index = int(lineno) - 1
        except (TypeError, ValueError):
            return None
        if 0 <= index < len(self.line_map):
            return self.line_map[index]
        return None

    def cell_for(self, lineno):
        '''Return the cell holding the given original line number.'''
        if lineno is None:
            return None
        for cell in self.cells:
            if cell.start_line <= lineno < (
                cell.start_line + cell.line_count
            ):
                return cell
        return None


def _source_text(source):
    '''Normalize the ``source`` field of a notebook cell.'''
    if isinstance(source, (list, tuple)):
        return ''.join(source)
    return source or ''


def _cell_field(cell, name, default=None):
    '''Read a field from a notebook cell.

    ``nbformat`` returns ``NotebookNode`` objects, but the CLI also accepts
    plain dictionaries in tests and in third-party code.
    '''
    try:
        return cell[name]
    except (KeyError, TypeError, IndexError):
        return getattr(cell, name, default)


def _notebook_cells(nb):
    '''Return the code cells of *nb*, in document order.

    Execution counts are deliberately not used for ordering: notebooks often
    contain cells that were run out of order. They are only kept as provenance
    metadata.
    '''
    cells = []
    chunks = []
    start_line = 1
    ordinal = 0
    for index, cell in enumerate(_cell_field(nb, 'cells', ()) or ()):
        if _cell_field(cell, 'cell_type') != 'code':
            continue
        source = _source_text(_cell_field(cell, 'source'))
        line_count = len(source.split('\n'))
        cells.append(
            _NotebookCell(
                index=index,
                ordinal=ordinal,
                cell_id=_cell_field(cell, 'id'),
                execution_count=_cell_field(cell, 'execution_count'),
                source=source,
                start_line=start_line,
                line_count=line_count,
            )
        )
        chunks.append(source)
        start_line += line_count
        ordinal += 1
    return cells, '\n'.join(chunks)


def _notebook_source(code, cells):
    '''Build a :class:`_NotebookSource` for *code*.

    *cells* must be expressed in the same coordinate system as *code*: line
    numbers in the resulting ``line_map`` are 1-based positions inside
    *code*, and every cell's ``start_line`` is relative to the same origin.
    '''
    stripped, line_map = strip_ipython_with_map(code)
    return _NotebookSource(stripped, cells, line_map)


def _single_cell_source(cell):
    '''Build a :class:`_NotebookSource` for one cell.

    The cell is rebased so that its first line is line 1, which matches the
    coordinates of the per-cell source.
    '''
    rebased = cell._replace(start_line=1)
    return _notebook_source(cell.source, [rebased])


def _cell_identity(cell):
    '''Return the stable identity of a cell.

    Cells saved with nbformat 4.5+ carry an ``id`` that survives reordering.
    Older notebooks do not, so the ordinal position among code cells is used.
    Two cells with the same source still get different identities, since the
    ordinal is positional.
    '''
    if cell.cell_id is not None:
        return cell.cell_id
    return cell.ordinal


def _annotate_block(block, notebook_source):
    '''Attach notebook cell information to a complexity block.'''
    start = notebook_source.original_line(block.lineno)
    end = notebook_source.original_line(block.endline)
    if start is None:
        return block
    if end is None or end < start:
        end = start

    replacements = {'lineno': start, 'endline': end}
    for attr in ('methods', 'closures', 'inner_classes'):
        inner = getattr(block, attr, None)
        if inner is not None:
            replacements[attr] = [
                _annotate_block(child, notebook_source) for child in inner
            ]
    annotated = block._replace(**replacements)

    cell = notebook_source.cell_for(start)
    if cell is not None:
        annotated.cell = _cell_identity(cell)
        annotated.cell_lineno = start - cell.start_line + 1
        # Blocks are defined by a single cell in practice. Do not let
        # ``cell_endline`` point outside of the cell though: if a block
        # somehow spans cells, report the part that belongs to its first cell.
        annotated.cell_endline = min(
            end - cell.start_line + 1, cell.line_count
        )
        annotated.execution_count = cell.execution_count
        if cell.cell_id is not None:
            annotated.cell_id = cell.cell_id
    return annotated


class Harvester(object):
    '''Base class defining the interface of a Harvester object.

    A Harvester has the following lifecycle:

    1. **Initialization**: `h = Harvester(paths, config)`

    2. **Execution**: `r = h.results`. `results` holds an iterable object.
       The first time `results` is accessed, `h.run()` is called. This method
       should not be subclassed. Instead, the :meth:`gobble` method should be
       implemented.

    3. **Reporting**: the methods *as_json* and *as_xml* return a string
       with the corresponding format. The method *to_terminal* is a generator
       that yields the lines to be printed in the terminal.

    This class is meant to be subclasses and cannot be used directly, since
    the methods :meth:`gobble`, :meth:`as_xml` and :meth:`to_terminal` are
    not implemented.
    '''

    def __init__(self, paths, config):
        '''Initialize the Harvester.

        *paths* is a list of paths to analyze.
        *config* is a :class:`~radon.cli.Config` object holding the
        configuration values specific to the Harvester.
        '''
        self.paths = paths
        self.config = config
        self._results = []

    def _iter_filenames(self):
        '''A wrapper around :func:`~radon.cli.tools.iter_filenames`.'''
        return iter_filenames(
            self.paths, self.config.exclude, self.config.ignore
        )

    def gobble(self, fobj):
        '''Subclasses must implement this method to define behavior.

        This method is called for every file to analyze. *fobj* is the file
        object. This method should return the results from the analysis,
        preferably a dictionary.
        '''
        raise NotImplementedError

    def _gobble_notebook(self, notebook_source):
        '''Analyze notebook code and let subclasses post-process the result.'''
        result = self.gobble(StringIO(notebook_source.code))
        return self._notebook_result(result, notebook_source)

    def _notebook_result(self, result, notebook_source):
        '''Hook for subclasses to attach notebook cell information.'''
        return result

    def run(self):
        '''Start the analysis. For every file, this method calls the
        :meth:`gobble` method. Results are yielded as tuple:
        ``(filename, analysis_results)``.
        '''
        for name in self._iter_filenames():
            with _open(name) as fobj:
                try:
                    if name.endswith('.ipynb'):
                        if SUPPORTS_IPYNB and self.config.include_ipynb:
                            nb = nbformat.read(
                                fobj, as_version=nbformat.NO_CONVERT
                            )
                            cells, doc = _notebook_cells(nb)
                            # Whole document
                            yield (
                                name,
                                self._gobble_notebook(
                                    _notebook_source(doc, cells)
                                ),
                            )

                            if self.config.ipynb_cells:
                                # Individual cells
                                for cell in cells:
                                    cell_name = "{0}:[{1}]".format(
                                        name, _cell_identity(cell)
                                    )
                                    try:
                                        result = self._gobble_notebook(
                                            _single_cell_source(cell)
                                        )
                                    except Exception as e:
                                        result = {'error': str(e)}
                                    yield (cell_name, result)
                    else:
                        yield (name, self.gobble(fobj))
                except Exception as e:
                    yield (name, {'error': str(e)})

    @property
    def results(self):
        '''This property holds the results of the analysis.

        The first time it is accessed, an iterator is returned. Its
        elements are cached into a list as it is iterated over. Therefore, if
        `results` is accessed multiple times after the first one, a list will
        be returned.
        '''

        def caching_iterator(it, r):
            '''An iterator that caches another iterator.'''
            for t in it:
                yield t
                r.append(t)

        if self._results:
            return self._results
        return caching_iterator(self.run(), self._results)

    def as_json(self):
        '''Format the results as JSON.'''
        return json.dumps(dict(self.results))

    def as_xml(self):
        '''Format the results as XML.'''
        raise NotImplementedError

    def as_md(self):
        '''Format the results as Markdown.'''
        raise NotImplementedError

    def as_codeclimate_issues(self):
        '''Format the results as Code Climate issues.'''
        raise NotImplementedError

    def to_terminal(self):
        '''Yields tuples representing lines to be printed to a terminal.

        The tuples have the following format: ``(line, args, kwargs)``.
        The line is then formatted with `line.format(*args, **kwargs)`.
        '''
        raise NotImplementedError


class CCHarvester(Harvester):
    '''A class that analyzes Python modules' Cyclomatic Complexity.'''

    def gobble(self, fobj):
        '''Analyze the content of the file object.'''
        r = cc_visit(fobj.read(), no_assert=self.config.no_assert)
        if self.config.show_closures:
            r = add_inner_blocks(r)
        return sorted_results(r, order=self.config.order)

    def _notebook_result(self, blocks, notebook_source):
        '''Map every block back to the notebook cell that defines it.'''
        if isinstance(blocks, dict) and 'error' in blocks:
            return blocks
        return [_annotate_block(block, notebook_source) for block in blocks]

    def _to_dicts(self):
        '''Format the results as a dictionary of dictionaries.'''
        result = {}
        for key, data in self.results:
            if 'error' in data:
                result[key] = data
                continue
            values = [
                v
                for v in map(cc_to_dict, data)
                if self.config.min <= v['rank'] <= self.config.max
            ]
            if values:
                result[key] = values
        return result

    def as_json(self):
        '''Format the results as JSON.'''
        return json.dumps(self._to_dicts())

    def as_xml(self):
        '''Format the results as XML. This is meant to be compatible with
        Jenkin's CCM plugin. Therefore not all the fields are kept.
        '''
        return dict_to_xml(self._to_dicts())

    def as_md(self):
        '''Format the results as Markdown.'''
        return dict_to_md(self._to_dicts())

    def as_codeclimate_issues(self):
        '''Format the result as Code Climate issues.'''
        return dict_to_codeclimate_issues(self._to_dicts(), self.config.min)

    def to_terminal(self):
        '''Yield lines to be printed in a terminal.'''
        average_cc = 0.0
        analyzed = 0
        for name, blocks in self.results:
            if 'error' in blocks:
                yield name, (blocks['error'],), {'error': True}
                continue
            res, cc, n = cc_to_terminal(
                blocks,
                self.config.show_complexity,
                self.config.min,
                self.config.max,
                self.config.total_average,
            )
            average_cc += cc
            analyzed += n
            if res:
                yield name, (), {}
                yield res, (), {'indent': 1}

        if (self.config.average or self.config.total_average) and analyzed:
            cc = average_cc / analyzed
            ranked_cc = cc_rank(cc)
            yield (
                '\n{0} blocks (classes, functions, methods) analyzed.',
                (analyzed,),
                {},
            )
            yield (
                'Average complexity: {0}{1} ({2}){3}',
                (RANKS_COLORS[ranked_cc], ranked_cc, cc, RESET),
                {},
            )


class RawHarvester(Harvester):
    '''A class that analyzes Python modules' raw metrics.'''

    headers = [
        'LOC',
        'LLOC',
        'SLOC',
        'Comments',
        'Single comments',
        'Multi',
        'Blank',
    ]

    def gobble(self, fobj):
        '''Analyze the content of the file object.'''
        return raw_to_dict(analyze(fobj.read()))

    def as_xml(self):
        '''Placeholder method. Currently not implemented.'''
        raise NotImplementedError('RawHarvester: cannot export results as XML')

    def to_terminal(self):
        '''Yield lines to be printed to a terminal.'''
        sum_metrics = collections.defaultdict(int)
        for path, mod in self.results:
            if 'error' in mod:
                yield path, (mod['error'],), {'error': True}
                continue
            yield path, (), {}
            for header in self.headers:
                value = mod[header.lower().replace(' ', '_')]
                yield '{0}: {1}', (header, value), {'indent': 1}
                sum_metrics[header] += value

            loc, comments = mod['loc'], mod['comments']
            yield '- Comment Stats', (), {'indent': 1}
            yield (
                '(C % L): {0:.0%}',
                (comments / (float(loc) or 1),),
                {'indent': 2},
            )
            yield (
                '(C % S): {0:.0%}',
                (comments / (float(mod['sloc']) or 1),),
                {'indent': 2},
            )
            yield (
                '(C + M % L): {0:.0%}',
                ((comments + mod['multi']) / (float(loc) or 1),),
                {'indent': 2},
            )

        if self.config.summary:
            _get = lambda k, v=0: sum_metrics.get(k, v)
            comments = float(_get('Comments'))
            yield '** Total **', (), {}
            for header in self.headers:
                yield '{0}: {1}', (header, sum_metrics[header]), {'indent': 1}

            yield '- Comment Stats', (), {'indent': 1}
            yield (
                '(C % L): {0:.0%}',
                (comments / (_get('LOC', 1) or 1),),
                {'indent': 2},
            )
            yield (
                '(C % S): {0:.0%}',
                (comments / (_get('SLOC', 1) or 1),),
                {'indent': 2},
            )
            yield (
                '(C + M % L): {0:.0%}',
                (
                    float(_get('Comments', 0) + _get('Multi'))
                    / (_get('LOC', 1) or 1),
                ),
                {'indent': 2},
            )


class MIHarvester(Harvester):
    '''A class that analyzes Python modules' Maintainability Index.'''

    def gobble(self, fobj):
        '''Analyze the content of the file object.'''
        mi = mi_visit(fobj.read(), self.config.multi)
        rank = mi_rank(mi)
        return {'mi': mi, 'rank': rank}

    @property
    def filtered_results(self):
        '''Filter results with respect with their rank.'''
        for key, value in self.results:
            if (
                'error' in value
                or self.config.min <= value['rank'] <= self.config.max
            ):
                yield (key, value)

    def _sort(self, results):
        if self.config.sort:
            return sorted(results, key=lambda el: el[1]['mi'])
        return results

    def as_json(self):
        '''Format the results as JSON.'''
        return json.dumps(dict(self.filtered_results))

    def as_xml(self):
        '''Placeholder method. Currently not implemented.'''
        raise NotImplementedError('Cannot export results as XML')

    def to_terminal(self):
        '''Yield lines to be printed to a terminal.'''
        for name, mi in self._sort(self.filtered_results):
            if 'error' in mi:
                yield name, (mi['error'],), {'error': True}
                continue
            rank = mi['rank']
            color = MI_RANKS[rank]
            to_show = ''
            if self.config.show:
                to_show = ' ({0:.2f})'.format(mi['mi'])
            yield '{0} - {1}{2}{3}{4}', (name, color, rank, to_show, RESET), {}


class HCHarvester(Harvester):
    """Computes the Halstead Complexity of Python modules."""

    def __init__(self, paths, config):
        super().__init__(paths, config)
        self.by_function = config.by_function

    def gobble(self, fobj):
        """Analyze the content of the file object."""
        code = fobj.read()
        return h_visit(code)

    def as_json(self):
        """Format the results as JSON."""
        result_dict = self._to_dicts()
        return json.dumps(result_dict)

    def to_terminal(self):
        """Yield lines to be printed to the terminal."""
        if self.by_function:
            for name, res in self.results:
                yield "{}:".format(name), (), {}
                for (name, report) in res.functions:
                    yield "{}:".format(name), (), {"indent": 1}
                    for msg in hal_report_to_terminal(report, 1):
                        yield msg
        else:
            for name, res in self.results:
                yield "{}:".format(name), (), {}
                for msg in hal_report_to_terminal(res.total, 0):
                    yield msg

    def _to_dicts(self):
        '''Format the results as a dictionary of dictionaries.'''
        result = {}
        for filename, results in self.results:
            if 'error' in results:
                result[filename] = results
            else:
                result[filename] = {}
                for k, v in results._asdict().items():
                    if k == "functions":
                        result[filename]["functions"] = {key: val._asdict() for key, val in v}
                    else:
                        result[filename][k] = v._asdict()

        return result


def hal_report_to_terminal(report, base_indent=0):
    """Yield lines from the HalsteadReport to print to the terminal."""
    yield "h1: {}".format(report.h1), (), {"indent": 1 + base_indent}
    yield "h2: {}".format(report.h2), (), {"indent": 1 + base_indent}
    yield "N1: {}".format(report.N1), (), {"indent": 1 + base_indent}
    yield "N2: {}".format(report.N2), (), {"indent": 1 + base_indent}
    yield "vocabulary: {}".format(report.vocabulary), (), {
        "indent": 1 + base_indent
    }
    yield "length: {}".format(report.length), (), {"indent": 1 + base_indent}
    yield "calculated_length: {}".format(report.calculated_length), (), {
        "indent": 1 + base_indent
    }
    yield "volume: {}".format(report.volume), (), {"indent": 1 + base_indent}
    yield "difficulty: {}".format(report.difficulty), (), {
        "indent": 1 + base_indent
    }
    yield "effort: {}".format(report.effort), (), {"indent": 1 + base_indent}
    yield "time: {}".format(report.time), (), {"indent": 1 + base_indent}
    yield "bugs: {}".format(report.bugs), (), {"indent": 1 + base_indent}
