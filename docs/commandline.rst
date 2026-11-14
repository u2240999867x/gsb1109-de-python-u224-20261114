Command-line Usage
==================

Radon currently has four commands:

    * :command:`cc`: compute Cyclomatic Complexity
    * :command:`raw`: compute raw metrics
    * :command:`mi`: compute Maintainability Index
    * :command:`hal`: compute Halstead complexity metrics

Additionally, three commands compare snapshots over time:

    * :command:`snapshot`: save a function-level report for later comparison
    * :command:`diff`: compare two snapshots and explain every continuation
    * :command:`trend`: track complexity across an ordered series of snapshots

.. note::
    On some systems, such as Windows, the default encoding is not UTF-8. If you
    are using Unicode characters in your Python file and want to analyze it
    with Radon, you'll have to set the `RADONFILESENCODING` environment
    variable to `UTF-8`.

Radon configuration files
-------------------------

When using radon regularly, you may want to specify default argument values in a configuration file.
For example, all of the radon commands have a ``--exclude`` and ``--ignore`` argument on the command-line.

Radon will look for the following files to determine default arguments:

* ``radon.cfg``
* ``pyproject.toml`` (with optional `toml` install on Python <3.11)
* ``setup.cfg``
* ``~/.radon.cfg``

Any radon configuration will be given in the INI-format, under the section ``[radon]``.

For example:

.. code-block:: ini

   [radon]
   exclude = test_*.py
   cc_min = B

Usage with Jupyter Notebooks
----------------------------

Radon can be used with ``.ipynb`` files to inspect code metrics for Python cells. Any ``%`` macros will be ignored in the metrics.

.. note::

   Jupyter Notebook support requires the optional ``nbformat`` package. To install, run ``pip install nbformat``.

To enable scanning of Jupyter notebooks, add the ``--include-ipynb`` flag with any of the commands.

To enable reporting of individual cells, add the ``--ipynb-cells`` flag with any of the commands.

The :command:`cc` command
-------------------------

.. program:: cc

This command analyzes Python source files and compute Cyclomatic Complexity.
The output can be filtered by specifying the :option:`-n` and :option:`-x`
flags. By default, the complexity score is not displayed, the option
:option:`-s` (show complexity) toggles this behaviour. File or directories
exclusion is supported through glob patterns. Every positional argument is
interpreted as a path. The program then walks through its children and analyzes
Python files.
Every block will be ranked from A (best complexity score) to F (worst one).
Ranks corresponds to complexity scores as follows:

    ========== ====== =========================================
     CC score   Rank   Risk
    ========== ====== =========================================
     1 - 5      A      low - simple block
     6 - 10     B      low - well structured and stable block
     11 - 20    C      moderate - slightly complex block
     21 - 30    D      more than moderate - more complex block
     31 - 40    E      high - complex block, alarming
       41+      F      very high - error-prone, unstable block
    ========== ====== =========================================

Blocks are also classified into three types: functions, methods and classes.
They're listed by letter in the command output for convenience when scanning
through a longer list of blocks:

    ============ ========
     Block type   Letter
    ============ ========
     Function     F
     Method       M
     Class        C
    ============ ========

Options
+++++++

.. option:: -x, --max

   Set the maximum complexity rank to display, defaults to ``F``.

   Value can be set in a configuration file using the ``cc_max`` property.

.. option:: -n, --min

   Set the minimum complexity rank to display, defaults to ``A``.

   Value can be set in a configuration file using the ``cc_min`` property.

.. option:: -a, --average

   If given, at the end of the analysis show the average Cyclomatic
   Complexity. This option is influenced by :option:`-x, --max` and
   :option:`-n, --min` options.

   Value can be set in a configuration file using the ``average`` property.

.. option:: --total-average

   Like :option:`-a, --average`, but it is not influenced by `min` and `max`.
   Every analyzed block is counted, no matter whether it is displayed or not.

   Value can be set in a configuration file using the ``total_average`` property.

.. option:: -s, --show-complexity

   If given, show the complexity score along with its rank.

   Value can be set in a configuration file using the ``show_complexity`` property.

.. option:: -e, --exclude

   Exclude files only when their path matches one of these glob patterns.
   Usually needs quoting at the command line.

   This is a comma-separated list. ``--exclude a.py,b.py`` will skip both
   ``a.py`` and ``b.py``, but ``--exclude a.py --exclude b.py`` will
   **NOT** exclude ``a.py``.

   Value can be set in a configuration file using the ``exclude`` property.

.. option:: -i, --ignore

   Ignore directories when their name matches one of these glob patterns: radon
   won't even descend into them. By default, hidden directories (starting with
   '.') are ignored.

   This is a comma-separated list. ``--ignore a,b`` will ignore both ``a`` and
   ``b``, but ``--ignore a --ignore b`` will **NOT** ignore ``a``.

   Value can be set in a configuration file using the ``ignore`` property.

.. option:: -o, --order

   The ordering function for the results. Can be one of:

    * `SCORE`: order by cyclomatic complexity (descending):
    * `LINES`: order by line numbers;
    * `ALPHA`: order by block names (alphabetically).

   Value can be set in a configuration file using the ``order`` property.

.. option:: -j, --json

   If given, the results will be converted into JSON. This is useful in case
   you need to export the results to another application.

.. option:: --xml

   If given, the results will be converted into XML. Note that not all the
   information is kept. This is specifically targeted to Jenkin's plugin CCM.

.. option:: --md

   If given, the results will be converted into Markdown. Note that not all the
   information is kept.

.. option:: --no-assert

   Does not count assert statements when computing complexity. This is because
   Python can be run with an optimize flag which removes assert statements.

   Value can be set in a configuration file using the ``no_assert`` property.

.. option:: --include-ipynb

   Include the Python cells within IPython Notebooks in the reporting.

   Value can be set in a configuration file using the ``include_ipynb`` property.

.. option:: --ipynb-cells

   Report on individual cells in any .ipynb files.

   Value can be set in a configuration file using the ``ipynb_cells`` property.

.. option:: -O, --output-file

   Save output to the specified output file.

   Value can be set in a configuration file using the ``output_file`` property.

Examples
++++++++

::

    $ radon cc path

Radon will walk through the subdirectories of path and will analyze all
child nodes (every Python file it encounters).

::

    $ radon cc -e "path/tests*,path/docs/*" path

As in the above example, Radon will walk the directories, excluding paths
matching ``path/tests/*`` and ``path/docs/*``.

.. warning::

   Remember to quote the patterns, otherwise your shell might expand them!

Depending on the single cases, a more suitable alternative might be this::

    $ radon cc -i "docs,tests" path

::

    $ cat path/to/file.py | radon cc -

Setting the path to "-" will cause Radon to analyze code from stdin

::

    $ radon cc --min B --max E path

Here Radon will only display blocks ranked between B and E (i.e. from ``CC=6``
to ``CC=40``).


The :command:`mi` command
-------------------------

.. program:: mi

This command analyzes Python source code files and compute the Maintainability
Index score.
Every positional argument is treated as a starting point from which to walk
looking for Python files (as in the :command:`cc` command). Paths can be
excluded with the :option:`-e` option.
The Maintainability Index is always in the range 0-100. MI is ranked as
follows:

    ========== ====== =================
     MI score   Rank   Maintainability
    ========== ====== =================
     100 - 20    A     Very high
      19 - 10    B     Medium
       9 - 0     C     Extremely low
    ========== ====== =================


Options
+++++++

.. option:: -x, --max

   Set the maximum MI to display. Expects a letter between A-F. Defaults to ``C``.

   Value can be set in a configuration file using the ``mi_max`` property.

.. option:: -n, --min

   Set the minimum MI to display. Expects a letter between A-F. Defaults to ``A``.

   Value can be set in a configuration file using the ``mi_min`` property.

.. option:: -e, --exclude

   Exclude files only when their path matches one of these glob patterns.
   Usually needs quoting at the command line.

   Value can be set in a configuration file using the ``exclude`` property.

.. option:: -i, --ignore

   Ignore directories when their name matches one of these glob patterns: radon
   won't even descend into them. By default, hidden directories (starting with
   '.') are ignored.

   Value can be set in a configuration file using the ``ignore`` property.

.. option:: -m, --multi

   If given, Radon will not count multiline strings as comments.
   Most of the time this is safe since multiline strings are used as functions
   docstrings, but one should be aware that their use is not limited to that
   and sometimes it would be wrong to count them as comment lines.

   Value can be set in a configuration file using the ``multi`` property.

.. option:: -s, --show

   If given, the actual MI value is shown in results, alongside the rank.

   Value can be set in a configuration file using the ``show_mi`` property.

.. option:: -j, --json

   Format results in JSON.

.. option:: --include-ipynb

   Include the Python cells within IPython Notebooks in the reporting.

   Value can be set in a configuration file using the ``include_ipynb`` property.

.. option:: --ipynb-cells

   Report on individual cells in any .ipynb files.

   Value can be set in a configuration file using the ``ipynb_cells`` property.

.. option:: -O, --output-file

   Save output to the specified output file.

   Value can be set in a configuration file using the ``output_file`` property.


Examples
++++++++

::

    $ radon mi path1 path2

Analyze every Python file under *path1* or *path2*. It checks recursively in
every subdirectory.


::

    $ radon mi path1 -e "path1/tests/*"

Like the previous example, but excluding from the analysis every path that
matches `path1/tests/*`.

::

    $ radon mi -m path1

Like the previous examples, but does not count multiline strings as comments.


The :command:`raw` command
--------------------------

.. program:: raw

This command analyzes the given Python modules in order to compute raw metrics.
These include:

    * **LOC**: the total number of lines of code
    * **LLOC**: the number of logical lines of code
    * **SLOC**: the number of source lines of code - not necessarily
      corresponding to the **LLOC** [Wikipedia]_
    * **comments**: the number of Python comment lines (i.e. only single-line
      comments ``#``)
    * **multi**: the number of lines representing multi-line strings
    * **blank**: the number of blank lines (or whitespace-only ones)

The equation :math:`sloc + multi + single comments + blank = loc` should always
hold.

.. [Wikipedia] More information on **LOC**, **SLOC**, **LLOC** here: http://en.wikipedia.org/wiki/Source_lines_of_code


Options
+++++++

.. option:: -e, --exclude

   Exclude files only when their path matches one of these glob patterns.
   Usually needs quoting at the command line.

   Value can be set in a configuration file using the ``exclude`` property.

.. option:: -i, --ignore

   Ignore directories when their name matches one of these glob patterns: radon
   won't even descend into them. By default, hidden directories (starting with
   '.') are ignored.

   Value can be set in a configuration file using the ``ignore`` property.

.. option:: -s, --summary

   If given, at the end of the analysis a summary of the gathered
   metrics will be shown.

.. option:: -j, --json

   If given, the results will be converted into JSON. Note that the JSON export
   does not include the summary (enabled with the option `-s, --summary`).

.. option:: -O, --output-file

   Save output to the specified output file.

   Value can be set in a configuration file using the ``output_file`` property.

.. option:: --include-ipynb

   Include the Python cells within IPython Notebooks in the reporting.

   Value can be set in a configuration file using the ``include_ipynb`` property.

.. option:: --ipynb-cells

   Report on individual cells in any .ipynb files.

   Value can be set in a configuration file using the ``ipynb_cells`` property.

Examples
++++++++

::

    $ radon raw path1 path2

Analyze every Python file under *path1* or *path2*. It checks recursively in
every subdirectory.

::

    $ radon raw path1 -e "path1/tests/*"

Like the previous example, but excluding from the analysis every path that
matches ``path1/tests/*``.


The :command:`hal` command
--------------------------

.. program:: hal

This command analyzes Python source files and computes their Halstead
complexity metrics. Files can be analyzed as wholes, or in terms of their
top-level functions with the :option:`-f` flag.

Excluding files or directories is supported through glob patterns with the
:option:`-e` flag. Every positional argument is interpreted as a path. The
program walks through its children and analyzes Python files.

Options
+++++++

.. option:: -f, --functions

   Compute the metrics on the *function* level, as opposed to the *file* level.

   Value can be set in a configuration file using the ``functions`` property.

.. option:: -e, --exclude

   Exclude files when their path matches one of these glob patterns. Usually
   needs quoting at the command line.

   Value can be set in a configuration file using the ``exclude`` property.

.. option:: -i, --ignore

   Refuse to descend into directories that match any of these glob patterns. By
   default, hidden directories (starting with '.') are ignored.

   Value can be set in a configuration file using the ``ignore`` property.

.. option:: -j, --json

   Convert results into JSON. This is useful for exporting results to another
   application.

.. option:: -O, --output-file

   Save output to the specified output file.

   Value can be set in a configuration file using the ``output_file`` property.

.. option:: --include-ipynb

   Include the Python cells within IPython Notebooks in the reporting.

   Value can be set in a configuration file using the ``include_ipynb`` property.

.. option:: --ipynb-cells

   Report on individual cells in any .ipynb files.

   Value can be set in a configuration file using the ``ipynb_cells`` property.

Examples
++++++++

::

    $ radon hal file.py

Radon will analyze the given file.


::

    $ radon hal path/

Radon will walk through the subdirectories of ``path/`` and analyze all child
nodes (every Python file it encounters).

::

    $ radon hal -e 'path/tests*,path/docs/*' path/

As in the above example, Radon will walk the directories, excluding paths
matching ``path/tests/*`` and ``path/docs/*``.

.. warning::

   Remember to quote the patterns, otherwise your shell might expand them!

Depending on the single cases, a more suitable alternative might be this::

    $ radon hal -i "docs,tests" path

::

    $ radon hal - < path/to/file.py

Setting the path to "-" will cause Radon to analyze code from stdin.


Comparing source snapshots
--------------------------

A weekly report often compares two revisions of a tree. If a function was
only renamed or moved, comparing its position would produce one deletion and
one addition and make the complexity delta meaningless. The snapshot commands
instead compute a set of explicit **continuation relations** between two
revisions and only rank complexity changes for relations that have a single,
defensible predecessor.

Every relation has one of these statuses:

    ============ ===========================================================
     Status       Meaning
    ============ ===========================================================
     grounded     Evidence-backed one-to-one continuation. Only these are
                  eligible for complexity ranking.
     split        One source block became several targets. Every target keeps
                  its source relation, so provenance is not lost.
     ambiguous    Several continuations are equally plausible. The candidates
                  are listed and none of them is ranked.
    ============ ===========================================================

Matching is performed in this order:

1. explicit ``--rename`` and ``--split`` mappings;
2. same file, same qualified name, and identical structure;
3. same file and qualified name with an edited body;
4. a unique structurally identical block (covers a rename and file move);
5. a unique qualified name under a different file (covers a move);
6. a conservative split, when several new functions are assembled solely
   from statements of one old function.

Two identical wrapper functions are never assigned arbitrarily: if both could
be the continuation, the relation is reported as ambiguous and excluded from
ranking. Structural fingerprints ignore local identifiers, parameter names,
and line numbers, but retain operators, constants, attribute names, and
control flow.

The :command:`snapshot` command
-------------------------------

.. program:: snapshot

Save a function-level report as JSON. Saved reports are much smaller than
source trees and can be kept as weekly artifacts:

::

    $ radon snapshot src w42.json
    $ git checkout w43
    $ radon snapshot src w43.json
    $ radon diff w42.json w43.json

The input may also be produced by :command:`cc` with ``--json``.

The :command:`diff` command
---------------------------

.. program:: diff

.. option:: -r, --rename <str>

   Declare an explicit continuation, as ``old=new``. Repeat the option for
   several mappings. References may be ``path::qualname`` or a bare qualified
   name. The value may contain several comma-separated targets, which declares
   a split.

   ::

       $ radon diff w42 w43 -r 'core.py::old_name=utils.py::new_name'

.. option:: --split <str>

   Declare an explicit one-to-many split, as ``old=first,second``.

   ::

       $ radon diff w42 w43 --split 'pipeline.process=steps.add_all,steps.double'

.. option:: -j, --json

   Emit the full report as JSON: every relation, every evidence string, the
   ambiguity groups, the split groups, the ranked deltas, and the trend
   segments.

.. option:: --only-changed

   Rank only continuations whose complexity changed.

.. option:: --no-zero

   Hide continuations whose complexity did not change.

.. option:: -O, --output-file <str>

   Save output to the specified output file.

The :command:`trend` command
----------------------------

.. program:: trend

Give the snapshots in chronological order. A curve is drawn only across
grounded one-to-one continuations. When a split or an ambiguous predecessor
breaks the chain, the two sides are reported as separate lineages with the
reason; the tool never draws a line through an unsupported match.

::

    $ radon trend w40 w41 w42 w43 -l 'week 40,week 41,week 42,week 43'

.. option:: -l, --labels <str>

   Comma-separated labels for the snapshots, useful in JSON reports.

.. option:: -j, --json

   Emit each trend with its separate segments and break records.

Trend statuses are:

    ============== =========================================================
     Status         Meaning
    ============== =========================================================
     connected      A contiguous curve spanning the whole series.
     partial        Contiguous, but starts after or ends before a break.
     broken         The curve contains a gap.
     isolated       A single point with no connected neighbour.
    ============== =========================================================
