'''This module implements a content-addressed result cache for the
Harvester classes.

Results are keyed by the digest of the analyzed source text, so unchanged
files can be reused across runs even when they are renamed. Every harvester
stores its results under its own cache key, which also encodes the
configuration values that influence the analysis (e.g. ``no_assert`` for
CCHarvester or ``multi`` for MIHarvester). This way a single file can hold
results for several rule sets at once, and changing a relevant setting
transparently invalidates the cached values.
'''

import hashlib
import json
import os

from radon.metrics import Halstead, HalsteadReport
from radon.visitors import Class, Function

CACHE_VERSION = 1


def digest_text(text):
    '''Return the content digest of the analyzed source *text*.'''
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class ResultCache(object):
    '''A JSON file-backed cache mapping ``(digest, key)`` pairs to results.

    *path* is the location of the cache file. A missing or corrupt file is
    treated as an empty cache.
    '''

    def __init__(self, path):
        self.path = path
        self.entries = {}
        self._dirty = False
        if path and os.path.exists(path):
            try:
                with open(path) as fobj:
                    data = json.load(fobj)
                if data.get('version') == CACHE_VERSION:
                    entries = data.get('entries', {})
                    if isinstance(entries, dict):
                        self.entries = entries
            except (OSError, ValueError, AttributeError):
                self.entries = {}

    def get(self, digest, key):
        '''Return the cached value for *(digest, key)* or `None`.'''
        return self.entries.get(digest, {}).get(key)

    def set(self, digest, key, value):
        '''Store *value* under *(digest, key)*. *value* must be JSON
        serializable.'''
        self.entries.setdefault(digest, {})[key] = value
        self._dirty = True

    def save(self):
        '''Persist the cache to disk, if anything changed.'''
        if not self.path or not self._dirty:
            return
        directory = os.path.dirname(os.path.abspath(self.path))
        if not os.path.isdir(directory):
            os.makedirs(directory)
        tmp_path = self.path + '.tmp'
        with open(tmp_path, 'w') as fobj:
            json.dump(
                {'version': CACHE_VERSION, 'entries': self.entries}, fobj
            )
        if hasattr(os, 'replace'):
            os.replace(tmp_path, self.path)
        else:  # pragma: no cover
            os.rename(tmp_path, self.path)
        self._dirty = False


def cc_block_to_data(block):
    '''Convert a CC block (Function or Class) into JSON-serializable data.'''
    if isinstance(block, Class):
        return {
            'kind': 'class',
            'name': block.name,
            'lineno': block.lineno,
            'col_offset': block.col_offset,
            'endline': block.endline,
            'methods': [cc_block_to_data(m) for m in block.methods],
            'inner_classes': [
                cc_block_to_data(c) for c in block.inner_classes
            ],
            'real_complexity': block.real_complexity,
        }
    return {
        'kind': 'function',
        'name': block.name,
        'lineno': block.lineno,
        'col_offset': block.col_offset,
        'endline': block.endline,
        'is_method': block.is_method,
        'classname': block.classname,
        'closures': [cc_block_to_data(c) for c in block.closures],
        'complexity': block.complexity,
    }


def data_to_cc_block(data):
    '''Rebuild a CC block (Function or Class) from its cached data.'''
    if data['kind'] == 'class':
        return Class(
            name=data['name'],
            lineno=data['lineno'],
            col_offset=data['col_offset'],
            endline=data['endline'],
            methods=[data_to_cc_block(m) for m in data['methods']],
            inner_classes=[
                data_to_cc_block(c) for c in data['inner_classes']
            ],
            real_complexity=data['real_complexity'],
        )
    return Function(
        name=data['name'],
        lineno=data['lineno'],
        col_offset=data['col_offset'],
        endline=data['endline'],
        is_method=data['is_method'],
        classname=data['classname'],
        closures=[data_to_cc_block(c) for c in data['closures']],
        complexity=data['complexity'],
    )


def halstead_to_data(result):
    '''Convert a Halstead result into JSON-serializable data.'''
    return {
        'total': result.total._asdict(),
        'functions': [
            [name, report._asdict()] for name, report in result.functions
        ],
    }


def data_to_halstead(data):
    '''Rebuild a Halstead result from its cached data.'''
    return Halstead(
        total=HalsteadReport(**data['total']),
        functions=[
            (name, HalsteadReport(**report))
            for name, report in data['functions']
        ],
    )
