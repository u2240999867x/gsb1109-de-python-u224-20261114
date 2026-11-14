"""Tests for installer.lookup"""

import zipfile
from importlib.machinery import EXTENSION_SUFFIXES

import pytest

from installer.lookup import (
    ModuleResolver,
    SearchRoot,
    _as_module_file,
    _classify_record_path,
    _split_module_name,
    resolve_module,
)
from installer.sources import WheelFile


def make_wheel(tmp_path, name, files, version="1.0.0"):
    """Build a wheel whose RECORD lists every provided file."""
    dist_info = f"{name}-{version}.dist-info"
    path = tmp_path / f"{name}-{version}-py3-none-any.whl"
    all_files = dict(files)
    all_files[f"{dist_info}/WHEEL"] = (
        b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    )
    all_files[f"{dist_info}/METADATA"] = (
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode()
    )
    record_lines = [f"{record_path},," for record_path in sorted(all_files)]
    record_lines.append(f"{dist_info}/RECORD,,")
    all_files[f"{dist_info}/RECORD"] = "\n".join(record_lines).encode()

    with zipfile.ZipFile(path, "w") as archive:
        for record_path, content in all_files.items():
            archive.writestr(record_path, content)
    return path


def open_root(tmp_path, root_name, wheels):
    """Index wheels described as ``(wheel-name, {path: content})`` pairs."""
    root = SearchRoot(root_name)
    for wheel_name, files in wheels:
        wheel_path = make_wheel(tmp_path, wheel_name, files)
        with WheelFile.open(wheel_path) as source:
            root.add_wheel(source)
    return root


class TestSplitModuleName:
    def test_splits_dotted_name(self):
        assert _split_module_name("pkg.sub.mod") == ("pkg", "sub", "mod")

    def test_rejects_empty_name(self):
        with pytest.raises(ValueError, match="must not be empty"):
            _split_module_name("")

    @pytest.mark.parametrize("name", ["1pkg", "pkg.-sub", "pkg..sub", "pkg/sub"])
    def test_rejects_invalid_identifier(self, name):
        with pytest.raises(ValueError, match="not a valid dotted module name"):
            _split_module_name(name)


class TestClassifyRecordPath:
    data_dir = "demo-1.0.0.data"

    def test_passes_through_plain_paths(self):
        assert _classify_record_path("pkg/mod.py", self.data_dir) == "pkg/mod.py"

    def test_skips_dist_info(self):
        assert (
            _classify_record_path("demo-1.0.0.dist-info/RECORD", self.data_dir) is None
        )

    def test_skips_pycache(self):
        path = "pkg/__pycache__/mod.cpython-312.pyc"
        assert _classify_record_path(path, self.data_dir) is None

    def test_maps_purelib_data_dir(self):
        path = f"{self.data_dir}/purelib/pkg/mod.py"
        assert _classify_record_path(path, self.data_dir) == "pkg/mod.py"

    def test_maps_platlib_data_dir(self):
        path = f"{self.data_dir}/platlib/pkg/mod.py"
        assert _classify_record_path(path, self.data_dir) == "pkg/mod.py"

    def test_skips_other_data_schemes(self):
        path = f"{self.data_dir}/scripts/tool"
        assert _classify_record_path(path, self.data_dir) is None

    def test_skips_data_dir_without_payload(self):
        assert _classify_record_path(f"{self.data_dir}/purelib", self.data_dir) is None


class TestAsModuleFile:
    def test_classifies_source(self, tmp_path):
        wheel_path = make_wheel(tmp_path, "demo", {"pkg/mod.py": b""})
        with WheelFile.open(wheel_path) as source:
            root = SearchRoot("root")
            artifact = root.add_wheel(source)
        result = _as_module_file(artifact, "pkg/mod.py", "pkg/mod.py")
        assert result is not None
        assert result.kind == "source"
        assert result.artifact is artifact

    def test_classifies_extension_and_bytecode(self, tmp_path):
        extension_path = f"pkg/ext{EXTENSION_SUFFIXES[0]}"
        wheel_path = make_wheel(
            tmp_path,
            "demo",
            {extension_path: b"", "pkg/only.pyc": b""},
        )
        with WheelFile.open(wheel_path) as source:
            root = SearchRoot("root")
            artifact = root.add_wheel(source)
        extension = _as_module_file(artifact, extension_path, extension_path)
        bytecode = _as_module_file(artifact, "pkg/only.pyc", "pkg/only.pyc")
        assert extension is not None and extension.kind == "extension"
        assert bytecode is not None and bytecode.kind == "bytecode"

    def test_rejects_non_module_suffix(self, tmp_path):
        wheel_path = make_wheel(tmp_path, "demo", {"pkg/data.txt": b""})
        with WheelFile.open(wheel_path) as source:
            root = SearchRoot("root")
            artifact = root.add_wheel(source)
        assert _as_module_file(artifact, "pkg/data.txt", "pkg/data.txt") is None


class TestResolution:
    def test_regular_package_and_module(self, tmp_path):
        root = open_root(
            tmp_path,
            "site",
            [("demo", {"demo/__init__.py": b"", "demo/helpers.py": b""})],
        )
        resolver = ModuleResolver([root])

        package = resolver.resolve("demo")
        assert package.found and package.kind == "package"
        assert package.search_root == "site"
        assert package.origin is not None
        assert package.origin.installed_path == "demo/__init__.py"
        assert package.origin.artifact.distribution == "demo"

        module = resolver.resolve("demo.helpers")
        assert module.found and module.kind == "module"
        assert module.origin is not None
        assert module.origin.installed_path == "demo/helpers.py"
        assert module.shadowed == ()

    def test_namespace_split_across_roots(self, tmp_path):
        first = open_root(
            tmp_path,
            "first",
            [("alpha", {"ns/pkg_a/__init__.py": b"", "ns/pkg_a/a.py": b""})],
        )
        second = open_root(
            tmp_path,
            "second",
            [("beta", {"ns/pkg_b/__init__.py": b""})],
        )
        resolver = ModuleResolver([first, second])

        namespace = resolver.resolve("ns")
        assert namespace.found and namespace.kind == "namespace"
        assert namespace.origin is None and namespace.search_root is None
        layer = namespace.trace[0]
        assert layer.selected is None
        assert layer.portions == ("first:.", "second:.")
        assert [candidate.search_root for candidate in layer.candidates] == [
            "first",
            "second",
        ]
        assert all(candidate.kind == "namespace" for candidate in layer.candidates)

        sub_a = resolver.resolve("ns.pkg_a")
        assert sub_a.found and sub_a.search_root == "first"
        assert sub_a.origin is not None
        assert sub_a.origin.installed_path == "ns/pkg_a/__init__.py"
        assert sub_a.trace[1].portions == ("first:ns", "second:ns")

        sub_b = resolver.resolve("ns.pkg_b")
        assert sub_b.found and sub_b.search_root == "second"

    def test_later_regular_package_shadows_earlier_namespace(self, tmp_path):
        first = open_root(
            tmp_path,
            "first",
            [("alpha", {"ns/only_here.py": b"", "ns/resource.txt": b""})],
        )
        second = open_root(
            tmp_path,
            "second",
            [("beta", {"ns/__init__.py": b"", "ns/real.py": b""})],
        )
        resolver = ModuleResolver([first, second])

        result = resolver.resolve("ns")
        assert result.found and result.kind == "package"
        assert result.search_root == "second"
        shadowed_namespace = [
            candidate for candidate in result.shadowed if candidate.kind == "namespace"
        ]
        assert len(shadowed_namespace) == 1
        assert shadowed_namespace[0].search_root == "first"
        assert shadowed_namespace[0].reason == "shadowed-by-regular-package"

        real = resolver.resolve("ns.real")
        assert real.found and real.search_root == "second"
        assert real.trace[1].portions == ("second:ns",)

        taken_over = resolver.resolve("ns.only_here")
        assert not taken_over.found
        assert taken_over.kind is None and taken_over.origin is None

    def test_package_beats_same_named_module_in_one_root(self, tmp_path):
        root = open_root(
            tmp_path,
            "site",
            [("demo", {"dual.py": b"", "dual/__init__.py": b""})],
        )
        result = ModuleResolver([root]).resolve("dual")
        assert result.found and result.kind == "package"
        module_candidate = [
            candidate for candidate in result.shadowed if candidate.kind == "module"
        ]
        assert len(module_candidate) == 1
        assert module_candidate[0].reason == "package-precedence"

    def test_module_beats_same_named_namespace_directory(self, tmp_path):
        root = open_root(
            tmp_path,
            "site",
            [("demo", {"dual.py": b"", "dual/inside.txt": b""})],
        )
        result = ModuleResolver([root]).resolve("dual")
        assert result.found and result.kind == "module"
        namespace_candidate = [
            candidate for candidate in result.shadowed if candidate.kind == "namespace"
        ]
        assert len(namespace_candidate) == 1
        assert namespace_candidate[0].reason == "module-precedence"

    def test_suffix_precedence_extension_source_bytecode(self, tmp_path):
        extension_path = f"speed{EXTENSION_SUFFIXES[0]}"
        root = open_root(
            tmp_path,
            "site",
            [
                (
                    "demo",
                    {
                        extension_path: b"",
                        "speed.py": b"",
                        "speed.pyc": b"",
                    },
                )
            ],
        )
        result = ModuleResolver([root]).resolve("speed")
        assert result.found
        assert result.origin is not None and result.origin.kind == "extension"
        reasons = {
            candidate.file.installed_path: candidate.reason
            for candidate in result.shadowed
        }
        assert reasons == {
            "speed.py": "suffix-precedence",
            "speed.pyc": "suffix-precedence",
        }

    def test_earlier_search_root_wins(self, tmp_path):
        first = open_root(tmp_path, "first", [("a", {"pkg/__init__.py": b""})])
        second = open_root(tmp_path, "second", [("b", {"pkg/__init__.py": b""})])
        resolver = ModuleResolver([first, second])

        result = resolver.resolve("pkg")
        assert result.search_root == "first"
        assert len(result.shadowed) == 1
        assert result.shadowed[0].search_root == "second"
        assert result.shadowed[0].reason == "shadowed-by-regular-package"

    def test_swap_search_roots_changes_winner_layer_by_layer(self, tmp_path):
        first = open_root(
            tmp_path,
            "first",
            [("a", {"pkg/__init__.py": b"", "pkg/from_first.py": b""})],
        )
        second = open_root(
            tmp_path,
            "second",
            [("b", {"pkg/__init__.py": b"", "pkg/from_second.py": b""})],
        )
        resolver = ModuleResolver([first, second])

        before = resolver.resolve("pkg")
        after = resolver.resolve("pkg", root_order=["second", "first"])

        assert before.search_root == "first"
        assert after.search_root == "second"
        assert after.shadowed[0].search_root == "first"
        assert before.trace[0].portions == ("first:.", "second:.")
        assert after.trace[0].portions == ("second:.", "first:.")
        assert before.trace != after.trace

        sub_before = resolver.resolve("pkg.from_second")
        sub_after = resolver.resolve("pkg.from_second", root_order=["second", "first"])
        assert not sub_before.found
        assert sub_after.found and sub_after.search_root == "second"

    def test_module_file_takes_over_namespace_across_roots(self, tmp_path):
        first = open_root(
            tmp_path,
            "first",
            [("alpha", {"ns/asset.txt": b"", "ns/helper.txt": b""})],
        )
        second = open_root(tmp_path, "second", [("beta", {"ns.py": b""})])
        third = open_root(tmp_path, "third", [("gamma", {"ns/other.txt": b""})])
        resolver = ModuleResolver([first, second, third])

        result = resolver.resolve("ns")
        assert result.found and result.kind == "module"
        assert result.search_root == "second"
        namespace_losers = [
            candidate for candidate in result.shadowed if candidate.kind == "namespace"
        ]
        assert {candidate.search_root for candidate in namespace_losers} == {
            "first",
            "third",
        }
        assert {candidate.reason for candidate in namespace_losers} == {
            "shadowed-by-module"
        }

    def test_invalid_root_order(self, tmp_path):
        first = open_root(tmp_path, "first", [("a", {"pkg/__init__.py": b""})])
        second = open_root(tmp_path, "second", [("b", {"other/__init__.py": b""})])
        resolver = ModuleResolver([first, second])

        with pytest.raises(ValueError, match="every search root exactly once"):
            resolver.resolve("pkg", root_order=["first"])
        with pytest.raises(ValueError, match="every search root exactly once"):
            resolver.resolve("pkg", root_order=["first", "first"])

    def test_module_file_blocks_submodule_lookup(self, tmp_path):
        root = open_root(tmp_path, "site", [("demo", {"only.py": b""})])
        result = ModuleResolver([root]).resolve("only.anything")
        assert not result.found
        assert len(result.trace) == 1
        assert result.trace[0].selected is not None
        assert result.trace[0].selected.kind == "module"

    def test_missing_name_returns_not_found(self, tmp_path):
        root = open_root(tmp_path, "site", [("demo", {"demo/__init__.py": b""})])
        result = ModuleResolver([root]).resolve("absent")
        assert not result.found
        assert result.trace[0].candidates == ()
        assert result.trace[0].selected is None

    def test_later_wheel_overwrites_same_path_in_root(self, tmp_path):
        first_path = make_wheel(tmp_path, "first", {"shared/__init__.py": b"# one"})
        second_path = make_wheel(tmp_path, "second", {"shared/__init__.py": b"# two"})
        root = SearchRoot("site")
        with WheelFile.open(first_path) as source:
            first_artifact = root.add_wheel(source)
        with WheelFile.open(second_path) as source:
            second_artifact = root.add_wheel(source)

        assert [artifact.distribution for artifact in root.artifacts] == [
            "first",
            "second",
        ]
        result = ModuleResolver([root]).resolve("shared")
        assert result.origin is not None
        assert result.origin.artifact == second_artifact
        overwritten = [
            candidate
            for candidate in result.shadowed
            if candidate.reason == "overwritten-by-later-wheel"
        ]
        assert len(overwritten) == 1
        assert overwritten[0].file is not None
        assert overwritten[0].file.artifact == first_artifact

    def test_data_purelib_files_are_importable(self, tmp_path):
        root = open_root(
            tmp_path,
            "site",
            [
                (
                    "demo",
                    {
                        "demo-1.0.0.data/purelib/relocated/__init__.py": b"",
                        "demo-1.0.0.data/data/relocated/asset.txt": b"",
                    },
                )
            ],
        )
        result = ModuleResolver([root]).resolve("relocated")
        assert result.found and result.kind == "package"

    def test_nested_package_trace_portions(self, tmp_path):
        root = open_root(
            tmp_path,
            "site",
            [
                (
                    "demo",
                    {
                        "pkg/sub/mod.py": b"",
                        "pkg/sub/__init__.py": b"",
                        "pkg/__init__.py": b"",
                    },
                )
            ],
        )
        result = ModuleResolver([root]).resolve("pkg.sub.mod")
        assert result.found
        assert [layer.portions for layer in result.trace] == [
            ("site:.",),
            ("site:pkg",),
            ("site:pkg/sub",),
        ]

    def test_resolve_module_helper(self, tmp_path):
        root = open_root(tmp_path, "site", [("demo", {"demo/__init__.py": b""})])
        result = resolve_module("demo", [root])
        assert result.found and result.origin is not None
        assert result.origin.artifact.distribution == "demo"

    def test_constructor_indexes_wheels(self, tmp_path):
        wheel_path = make_wheel(tmp_path, "demo", {"demo/__init__.py": b""})
        with WheelFile.open(wheel_path) as source:
            root = SearchRoot("site", wheels=[source])
        result = ModuleResolver([root]).resolve("demo")
        assert result.found
        assert root.artifacts[0].distribution == "demo"
        assert root.artifacts[0].filename == "demo-1.0.0.whl"

    def test_duplicate_search_root_names_are_rejected(self, tmp_path):
        first = open_root(tmp_path, "same", [("a", {"pkg/__init__.py": b""})])
        second = open_root(tmp_path, "same", [("b", {"other.py": b""})])
        with pytest.raises(ValueError, match="unique names"):
            ModuleResolver([first, second])

    def test_nested_namespace_drills_through_every_portion(self, tmp_path):
        first = open_root(
            tmp_path,
            "first",
            [("alpha", {"ns/inner/from_first.py": b"", "ns/inner/asset.txt": b""})],
        )
        second = open_root(
            tmp_path,
            "second",
            [
                (
                    "beta",
                    {
                        "ns/inner/from_second/__init__.py": b"",
                        "ns/other/__init__.py": b"",
                    },
                )
            ],
        )
        resolver = ModuleResolver([first, second])

        result = resolver.resolve("ns.inner.from_first")
        assert result.found and result.search_root == "first"
        assert [layer.portions for layer in result.trace] == [
            ("first:.", "second:."),
            ("first:ns", "second:ns"),
            ("first:ns/inner", "second:ns/inner"),
        ]
        assert all(layer.selected is None for layer in result.trace[:2])
        assert result.trace[2].selected is not None

        taken = resolver.resolve("ns.inner.from_second")
        assert taken.found and taken.search_root == "second"
