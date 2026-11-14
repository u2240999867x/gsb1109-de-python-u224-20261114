'''This module holds the base Harvester class and all its subclassess.'''

import collections
import json
import sys
from builtins import super

from radon.cli.cache import (
    ResultCache,
    cc_block_to_data,
    data_to_cc_block,
    data_to_halstead,
    digest_text,
    halstead_to_data,
)
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
    strip_ipython,
)
from radon.complexity import (
    add_inner_blocks,
    cc_rank,
    cc_visit,
    sorted_results,
)
import radon.complexity as cc_mod
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
        configuration values specific to the Harvester. If it contains a
        *cache_file* value, results are cached there and reused across runs
        for files whose content did not change.
        '''
        self.paths = paths
        self.config = config
        self._results = []
        cache_file = getattr(config, 'cache_file', None)
        self.cache = ResultCache(cache_file) if cache_file else None

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

    def cache_params(self):
        '''Return a tuple of ``(name, value)`` pairs describing the
        configuration values that influence the analysis results. Subclasses
        should override this method. It is used to build the cache key, so
        that results computed under different settings never collide.
        '''
        return ()

    @property
    def cache_key(self):
        '''The key under which results of this Harvester are cached.'''
        parts = [type(self).__name__]
        parts.extend(
            '{0}={1!r}'.format(name, value)
            for name, value in self.cache_params()
        )
        return '|'.join(parts)

    def to_cache(self, result):
        '''Convert an analysis result into a JSON-serializable value.'''
        return result

    def from_cache(self, data):
        '''Rebuild an analysis result from its cached value.'''
        return data

    def _gobble_text(self, text):
        '''Analyze *text*, reusing the cached result when possible.

        The cache is keyed by the content digest, so renamed files are
        reused, while the file list (and therefore exclusions) is always
        recomputed from the current paths.
        '''
        if self.cache is None:
            return self.gobble(StringIO(text))
        digest = digest_text(text)
        key = self.cache_key
        cached = self.cache.get(digest, key)
        if cached is not None:
            return self.from_cache(cached)
        result = self.gobble(StringIO(text))
        try:
            self.cache.set(digest, key, self.to_cache(result))
        except (TypeError, ValueError):
            # The result is not serializable: skip caching it.
            pass
        return result

    def _save_cache(self):
        '''Persist the result cache, if one is configured.'''
        if self.cache is not None:
            self.cache.save()

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
                            cells = [
                                cell.source
                                for cell in nb.cells
                                if cell.cell_type == 'code'
                            ]
                            # Whole document
                            doc = "\n".join(cells)
                            yield (name, self._gobble_text(strip_ipython(doc)))

                            if self.config.ipynb_cells:
                                # Individual cells
                                cellid = 0
                                for source in cells:
                                    yield (
                                        "{0}:[{1}]".format(name, cellid),
                                        self._gobble_text(
                                            strip_ipython(source)
                                        ),
                                    )
                                    cellid += 1
                    elif self.cache is None:
                        yield (name, self.gobble(fobj))
                    else:
                        yield (name, self._gobble_text(fobj.read()))
                except Exception as e:
                    yield (name, {'error': str(e)})
        self._save_cache()

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


def _order_name(order):
    '''Return a stable name for a :func:`~radon.complexity.sorted_results`
    ordering function. The built-in orderings are module-level lambdas, so
    they are identified by identity.'''
    name = getattr(order, '__name__', None)
    if name and name != '<lambda>':
        return name
    for candidate in ('SCORE', 'LINES', 'ALPHA'):
        if order is getattr(cc_mod, candidate, None):
            return candidate
    return str(order)


class CCHarvester(Harvester):
    '''A class that analyzes Python modules' Cyclomatic Complexity.'''

    def cache_params(self):
        '''CC results depend on `no_assert`, `show_closures` and `order`.'''
        return (
            ('no_assert', self.config.no_assert),
            ('show_closures', self.config.show_closures),
            ('order', _order_name(self.config.order)),
        )

    def to_cache(self, result):
        '''Convert CC blocks into JSON-serializable data.'''
        return [cc_block_to_data(block) for block in result]

    def from_cache(self, data):
        '''Rebuild CC blocks from their cached data.'''
        return [data_to_cc_block(block) for block in data]

    def gobble(self, fobj):
        '''Analyze the content of the file object.'''
        r = cc_visit(fobj.read(), no_assert=self.config.no_assert)
        if self.config.show_closures:
            r = add_inner_blocks(r)
        return sorted_results(r, order=self.config.order)

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

    def cache_params(self):
        '''MI results depend on `multi` (how comments are counted).'''
        return (('multi', self.config.multi),)

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

    def to_cache(self, result):
        """Convert a Halstead result into JSON-serializable data."""
        return halstead_to_data(result)

    def from_cache(self, data):
        """Rebuild a Halstead result from its cached data."""
        return data_to_halstead(data)

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
