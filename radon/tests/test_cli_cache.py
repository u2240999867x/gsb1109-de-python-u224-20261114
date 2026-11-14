import json
import os

import pytest

import radon.complexity as cc_mod
from radon.cli import Config
from radon.cli.cache import (
    ResultCache,
    cc_block_to_data,
    data_to_cc_block,
    data_to_halstead,
    digest_text,
    halstead_to_data,
)
from radon.cli.harvest import CCHarvester, HCHarvester, MIHarvester, RawHarvester
from radon.metrics import h_visit
from radon.visitors import Class, Function

SIMPLE_SOURCE = 'def f(x):\n    return x + 1\n'

BRANCHY_SOURCE = '''
class Foo(object):
    def bar(self):
        if self.x:
            return 1
        return 2

    def baz(self):
        for i in range(10):
            if i % 2:
                continue
        return i


def outer():
    def inner():
        return 42
    return inner()
'''

COMMENTED_SOURCE = '''
# a comment

def f():
    """Docstring."""
    # another comment
    return 1
'''


def make_cc_config(tmp_path, **overrides):
    values = dict(
        order=cc_mod.SCORE,
        no_assert=False,
        min='A',
        max='F',
        show_complexity=False,
        show_closures=False,
        average=False,
        total_average=False,
        exclude=None,
        ignore=None,
        include_ipynb=False,
        ipynb_cells=False,
        cache_file=str(tmp_path / 'cache.json'),
    )
    values.update(overrides)
    return Config(**values)


def make_mi_config(tmp_path, **overrides):
    values = dict(
        multi=True,
        min='A',
        max='C',
        show=True,
        sort=False,
        exclude=None,
        ignore=None,
        include_ipynb=False,
        ipynb_cells=False,
        cache_file=str(tmp_path / 'cache.json'),
    )
    values.update(overrides)
    return Config(**values)


def write(tmp_path, name, source):
    path = tmp_path / name
    path.write_text(source)
    return str(path)


def counting_gobble(harvester):
    '''Wrap the harvester's gobble method with a call counter.'''
    calls = []
    original = harvester.gobble

    def gobble(fobj):
        calls.append(True)
        return original(fobj)

    harvester.gobble = gobble
    return calls


class TestResultCache(object):
    def test_missing_file_is_empty(self, tmp_path):
        cache = ResultCache(str(tmp_path / 'nope.json'))
        assert cache.entries == {}
        assert cache.get('abc', 'key') is None

    def test_corrupt_file_is_empty(self, tmp_path):
        path = tmp_path / 'cache.json'
        path.write_text('not json {')
        cache = ResultCache(str(path))
        assert cache.entries == {}

    def test_roundtrip(self, tmp_path):
        path = str(tmp_path / 'cache.json')
        cache = ResultCache(path)
        cache.set('digest1', 'key1', {'value': 1})
        cache.set('digest1', 'key2', {'value': 2})
        cache.save()

        reloaded = ResultCache(path)
        assert reloaded.get('digest1', 'key1') == {'value': 1}
        assert reloaded.get('digest1', 'key2') == {'value': 2}
        assert reloaded.get('digest1', 'other') is None
        assert reloaded.get('other', 'key1') is None

    def test_save_without_changes_is_noop(self, tmp_path):
        path = tmp_path / 'cache.json'
        ResultCache(str(path)).save()
        assert not path.exists()

    def test_version_mismatch_is_empty(self, tmp_path):
        path = tmp_path / 'cache.json'
        path.write_text(json.dumps({'version': -1, 'entries': {'a': {}}}))
        cache = ResultCache(str(path))
        assert cache.entries == {}


class TestCodecs(object):
    def test_digest_text(self):
        assert digest_text('a') != digest_text('b')
        assert digest_text('a') == digest_text('a')

    def test_cc_function_roundtrip(self):
        inner = Function('inner', 2, 4, 3, False, None, [], 1)
        func = Function('outer', 1, 0, 4, False, None, [inner], 3)
        assert data_to_cc_block(cc_block_to_data(func)) == func

    def test_cc_class_roundtrip(self):
        method = Function('m', 2, 4, 3, True, 'C', [], 2)
        inner_cls = Class('I', 4, 4, 5, [], [], 1)
        cls = Class('C', 1, 0, 6, [method], [inner_cls], 7)
        assert data_to_cc_block(cc_block_to_data(cls)) == cls

    def test_cc_codec_is_json_serializable(self):
        method = Function('m', 2, 4, 3, True, 'C', [], 2)
        cls = Class('C', 1, 0, 6, [method], [], 7)
        data = cc_block_to_data(cls)
        assert json.loads(json.dumps(data)) == data

    def test_halstead_roundtrip(self):
        result = h_visit(BRANCHY_SOURCE)
        assert data_to_halstead(halstead_to_data(result)) == result


class TestCacheKeys(object):
    def test_keys_differ_between_harvesters(self, tmp_path):
        cc = CCHarvester([], make_cc_config(tmp_path))
        mi = MIHarvester([], make_mi_config(tmp_path))
        raw = RawHarvester([], make_mi_config(tmp_path))
        hal = HCHarvester(
            [], Config(by_function=False, **make_mi_config(tmp_path).config_values)
        )
        keys = {cc.cache_key, mi.cache_key, raw.cache_key, hal.cache_key}
        assert len(keys) == 4

    def test_cc_key_tracks_analysis_settings(self, tmp_path):
        base = CCHarvester([], make_cc_config(tmp_path))
        no_assert = CCHarvester(
            [], make_cc_config(tmp_path, no_assert=True)
        )
        closures = CCHarvester(
            [], make_cc_config(tmp_path, show_closures=True)
        )
        ordered = CCHarvester(
            [], make_cc_config(tmp_path, order=cc_mod.LINES)
        )
        keys = {
            base.cache_key,
            no_assert.cache_key,
            closures.cache_key,
            ordered.cache_key,
        }
        assert len(keys) == 4

    def test_mi_key_tracks_multi(self, tmp_path):
        multi = MIHarvester([], make_mi_config(tmp_path, multi=True))
        no_multi = MIHarvester([], make_mi_config(tmp_path, multi=False))
        assert multi.cache_key != no_multi.cache_key


class TestIncrementalCC(object):
    def test_unchanged_files_are_reused(self, tmp_path):
        target = write(tmp_path, 'mod.py', BRANCHY_SOURCE)
        config = make_cc_config(tmp_path)

        first = CCHarvester([target], config)
        calls = counting_gobble(first)
        first_results = list(first.results)
        assert len(calls) == 1

        second = CCHarvester([target], make_cc_config(tmp_path))
        calls = counting_gobble(second)
        second_results = list(second.results)
        assert calls == []
        assert second_results == first_results

    def test_changed_file_is_recomputed(self, tmp_path):
        target = write(tmp_path, 'mod.py', SIMPLE_SOURCE)
        first = CCHarvester([target], make_cc_config(tmp_path))
        list(first.results)

        write(tmp_path, 'mod.py', BRANCHY_SOURCE)
        second = CCHarvester([target], make_cc_config(tmp_path))
        calls = counting_gobble(second)
        results = list(second.results)
        assert len(calls) == 1
        clean = CCHarvester([target], make_cc_config(tmp_path, cache_file=None))
        assert results == list(clean.results)

    def test_rename_reuses_content_under_new_path(self, tmp_path):
        old = write(tmp_path, 'old.py', BRANCHY_SOURCE)
        first = CCHarvester([old], make_cc_config(tmp_path))
        first_results = dict(first.results)

        new = tmp_path / 'new.py'
        os.rename(old, str(new))
        second = CCHarvester([str(new)], make_cc_config(tmp_path))
        calls = counting_gobble(second)
        second_results = dict(second.results)

        assert calls == []
        assert list(second_results) == [str(new)]
        assert second_results[str(new)] == first_results[old]

    def test_rename_reevaluates_exclusions(self, tmp_path):
        old = write(tmp_path, 'old.py', BRANCHY_SOURCE)
        first = CCHarvester([old], make_cc_config(tmp_path))
        list(first.results)

        new = tmp_path / 'excluded.py'
        os.rename(old, str(new))
        config = make_cc_config(tmp_path, exclude='*excluded.py')
        second = CCHarvester([str(tmp_path)], config)
        assert list(second.results) == []

    def test_deleted_file_is_dropped(self, tmp_path):
        keep = write(tmp_path, 'keep.py', SIMPLE_SOURCE)
        gone = write(tmp_path, 'gone.py', BRANCHY_SOURCE)
        first = CCHarvester([str(tmp_path)], make_cc_config(tmp_path))
        assert sorted(dict(first.results)) == sorted([keep, gone])

        os.remove(gone)
        second = CCHarvester([str(tmp_path)], make_cc_config(tmp_path))
        assert sorted(dict(second.results)) == [keep]

    def test_cache_only_cc_cannot_serve_mi(self, tmp_path):
        target = write(tmp_path, 'mod.py', COMMENTED_SOURCE)
        cc = CCHarvester([target], make_cc_config(tmp_path))
        list(cc.results)

        mi = MIHarvester([target], make_mi_config(tmp_path))
        calls = counting_gobble(mi)
        cached_results = dict(mi.results)
        assert len(calls) == 1

        clean = MIHarvester(
            [target], make_mi_config(tmp_path, cache_file=None)
        )
        assert cached_results == dict(clean.results)

    def test_two_rule_sets_share_one_cache(self, tmp_path):
        target = write(tmp_path, 'mod.py', BRANCHY_SOURCE)
        cc = CCHarvester([target], make_cc_config(tmp_path))
        cc_results = list(cc.results)
        mi = MIHarvester([target], make_mi_config(tmp_path))
        mi_results = list(mi.results)

        cache = ResultCache(str(tmp_path / 'cache.json'))
        digest = digest_text(BRANCHY_SOURCE)
        assert set(cache.entries[digest]) == {
            CCHarvester([], make_cc_config(tmp_path)).cache_key,
            MIHarvester([], make_mi_config(tmp_path)).cache_key,
        }

        # Both rule sets are served from the cache on the next run.
        cc2 = CCHarvester([target], make_cc_config(tmp_path))
        mi2 = MIHarvester([target], make_mi_config(tmp_path))
        cc_calls = counting_gobble(cc2)
        mi_calls = counting_gobble(mi2)
        assert list(cc2.results) == cc_results
        assert list(mi2.results) == mi_results
        assert cc_calls == [] and mi_calls == []


class TestIncrementalMI(object):
    def test_delete_and_switch_multi_matches_clean_rescan(self, tmp_path):
        keep = write(tmp_path, 'keep.py', COMMENTED_SOURCE)
        gone = write(tmp_path, 'gone.py', BRANCHY_SOURCE)
        first = MIHarvester(
            [str(tmp_path)], make_mi_config(tmp_path, multi=True)
        )
        list(first.results)

        os.remove(gone)
        incremental = MIHarvester(
            [str(tmp_path)], make_mi_config(tmp_path, multi=False)
        )
        calls = counting_gobble(incremental)
        incremental_results = dict(incremental.results)

        clean = MIHarvester(
            [str(tmp_path)],
            make_mi_config(tmp_path, multi=False, cache_file=None),
        )
        clean_results = dict(clean.results)

        assert sorted(incremental_results) == [keep]
        assert incremental_results == clean_results
        # The `multi` switch invalidates the cached values.
        assert len(calls) == 1

    def test_same_multi_reuses_cache(self, tmp_path):
        target = write(tmp_path, 'mod.py', COMMENTED_SOURCE)
        first = MIHarvester([target], make_mi_config(tmp_path, multi=False))
        first_results = list(first.results)

        second = MIHarvester([target], make_mi_config(tmp_path, multi=False))
        calls = counting_gobble(second)
        assert list(second.results) == first_results
        assert calls == []


class TestIncrementalRawAndHal(object):
    def test_raw_reuses_cache(self, tmp_path):
        target = write(tmp_path, 'mod.py', COMMENTED_SOURCE)
        config = Config(
            summary=False,
            exclude=None,
            ignore=None,
            include_ipynb=False,
            ipynb_cells=False,
            cache_file=str(tmp_path / 'cache.json'),
        )
        first = RawHarvester([target], config)
        first_results = list(first.results)

        second = RawHarvester([target], config)
        calls = counting_gobble(second)
        assert list(second.results) == first_results
        assert calls == []

    def test_hal_reuses_cache(self, tmp_path):
        target = write(tmp_path, 'mod.py', BRANCHY_SOURCE)
        config = Config(
            by_function=False,
            exclude=None,
            ignore=None,
            include_ipynb=False,
            ipynb_cells=False,
            cache_file=str(tmp_path / 'cache.json'),
        )
        first = HCHarvester([target], config)
        first_results = list(first.results)

        second = HCHarvester([target], config)
        calls = counting_gobble(second)
        assert list(second.results) == first_results
        assert calls == []


class TestCacheDisabled(object):
    def test_no_cache_file_means_no_cache(self, tmp_path):
        target = write(tmp_path, 'mod.py', SIMPLE_SOURCE)
        config = make_cc_config(tmp_path, cache_file=None)
        harvester = CCHarvester([target], config)
        assert harvester.cache is None
        list(harvester.results)
        assert not (tmp_path / 'cache.json').exists()

    def test_errors_are_not_cached(self, tmp_path):
        target = write(tmp_path, 'mod.py', 'def broken(:\n')
        config = make_cc_config(tmp_path)
        first = CCHarvester([target], config)
        first_results = dict(first.results)
        assert 'error' in first_results[target]

        cache = ResultCache(str(tmp_path / 'cache.json'))
        assert cache.entries == {}
