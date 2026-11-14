import hashlib
from io import BytesIO

import pytest

from installer.plan import (
    CaseSensitivity,
    InstallPlan,
    TargetConflict,
    build_install_plan,
)
from installer.sources import WheelSource
from installer.utils import Scheme


def hash_and_size(data):
    return hashlib.sha256(data).hexdigest(), len(data)


class FakeWheelSource(WheelSource):
    def __init__(
        self,
        *,
        distribution="fancy",
        version="1.0.0",
        regular_files,
        root_is_purelib=True,
    ):
        super().__init__(distribution, version)
        self.regular_files = dict(regular_files)
        self.root_is_purelib = root_is_purelib

    @property
    def dist_info_filenames(self):
        return ["WHEEL"]

    def read_dist_info(self, filename):
        assert filename == "WHEEL"
        return (
            "Wheel-Version: 1.0\n"
            "Generator: magic (1.0.0)\n"
            f"Root-Is-Purelib: {'true' if self.root_is_purelib else 'false'}\n"
            "Tag: py3-none-any\n"
        )

    def validate_record(self) -> None:
        return

    def get_contents(self):
        for file, content in sorted(self.regular_files.items()):
            hashed, size = hash_and_size(content)
            record = (file, f"sha256={hashed}", str(size))
            with BytesIO(content) as stream:
                yield record, stream, False


def build(regular_files, case_sensitivity, **kwargs):
    source = FakeWheelSource(regular_files=regular_files)
    return build_install_plan(source, case_sensitivity, **kwargs)


CASE_COLLISION_FILES = {
    "fancy/Module.py": b"AAAA",
    "fancy/module.py": b"BBBB",
}


class TestBuildInstallPlan:
    def test_maps_archive_root_to_purelib(self):
        plan = build(
            {"fancy/__init__.py": b"print('hi')\n"},
            CaseSensitivity.CASE_SENSITIVE,
        )

        assert isinstance(plan, InstallPlan)
        (entry,) = plan.entries
        assert entry.source_path == "fancy/__init__.py"
        assert entry.scheme == Scheme("purelib")
        assert entry.destination_path == "fancy/__init__.py"
        assert entry.target_path == "purelib/fancy/__init__.py"
        assert plan.mapping["fancy/__init__.py"] is entry
        assert plan.conflicts == ()

    def test_maps_archive_root_to_platlib(self):
        source = FakeWheelSource(
            regular_files={"fancy/__init__.py": b""},
            root_is_purelib=False,
        )
        plan = build_install_plan(source, CaseSensitivity.CASE_SENSITIVE)

        assert plan.entries[0].scheme == Scheme("platlib")

    def test_expands_each_data_scheme(self):
        plan = build(
            {
                "fancy-1.0.0.data/purelib/fancy/a.py": b"a",
                "fancy-1.0.0.data/platlib/fancy/b.py": b"b",
                "fancy-1.0.0.data/headers/fancy/c.h": b"c",
                "fancy-1.0.0.data/scripts/fancy-d": b"d",
                "fancy-1.0.0.data/data/share/fancy/e.txt": b"e",
            },
            CaseSensitivity.CASE_SENSITIVE,
        )

        targets = {entry.source_path: entry.target_path for entry in plan.entries}
        assert targets == {
            "fancy-1.0.0.data/purelib/fancy/a.py": "purelib/fancy/a.py",
            "fancy-1.0.0.data/platlib/fancy/b.py": "platlib/fancy/b.py",
            "fancy-1.0.0.data/headers/fancy/c.h": "headers/fancy/c.h",
            "fancy-1.0.0.data/scripts/fancy-d": "scripts/fancy-d",
            "fancy-1.0.0.data/data/share/fancy/e.txt": "data/share/fancy/e.txt",
        }
        assert plan.conflicts == ()

    def test_detects_identical_contents_on_same_target(self):
        plan = build(
            {
                "fancy/__init__.py": b"same bytes\n",
                "fancy-1.0.0.data/purelib/fancy/__init__.py": b"same bytes\n",
            },
            CaseSensitivity.CASE_SENSITIVE,
        )

        (conflict,) = plan.conflicts
        assert isinstance(conflict, TargetConflict)
        assert conflict.scheme == Scheme("purelib")
        assert conflict.destination_path == "fancy/__init__.py"
        assert conflict.identical is True
        assert plan.content_conflicts == ()
        assert {entry.source_path for entry in conflict.entries} == {
            "fancy/__init__.py",
            "fancy-1.0.0.data/purelib/fancy/__init__.py",
        }

    def test_detects_content_conflict_on_same_target(self):
        plan = build(
            {
                "fancy/__init__.py": b"AAAA",
                "fancy-1.0.0.data/purelib/fancy/__init__.py": b"BBBB",
            },
            CaseSensitivity.CASE_SENSITIVE,
        )

        (conflict,) = plan.conflicts
        assert conflict.identical is False
        assert plan.content_conflicts == (conflict,)
        assert len({entry.digest for entry in conflict.entries}) == 2
        assert {entry.size for entry in conflict.entries} == {4}

    def test_different_schemes_never_collide(self):
        # Same relative path, but one lands in purelib and the other in
        # platlib, so the targets are distinct even when folded.
        plan = build(
            {
                "fancy/module.py": b"AAAA",
                "fancy-1.0.0.data/platlib/fancy/module.py": b"BBBB",
            },
            CaseSensitivity.CASE_INSENSITIVE,
        )

        assert plan.conflicts == ()
        assert {entry.target_path for entry in plan.entries} == {
            "purelib/fancy/module.py",
            "platlib/fancy/module.py",
        }

    def test_skips_record_and_pycache_members(self):
        source = FakeWheelSource(
            regular_files={
                "fancy/good.py": b"good\n",
                "fancy/__pycache__/good.cpython-312.pyc": b"bytecode\n",
                "fancy-1.0.0.dist-info/RECORD": b"record\n",
            }
        )
        plan = build_install_plan(source, CaseSensitivity.CASE_SENSITIVE)

        assert [entry.source_path for entry in plan.entries] == ["fancy/good.py"]


class TestCaseFolding:
    """Two same-size, different-content members colliding only by case."""

    files = CASE_COLLISION_FILES

    def test_case_sensitive_platform_keeps_distinct_targets(self):
        plan = build(self.files, CaseSensitivity.CASE_SENSITIVE)

        assert plan.conflicts == ()
        assert plan.content_conflicts == ()
        assert {entry.destination_path for entry in plan.entries} == {
            "fancy/Module.py",
            "fancy/module.py",
        }

    def test_case_insensitive_platform_reports_content_conflict(self):
        plan = build(self.files, CaseSensitivity.CASE_INSENSITIVE)

        (conflict,) = plan.conflicts
        assert conflict.identical is False
        assert plan.content_conflicts == (conflict,)
        assert {entry.source_path for entry in conflict.entries} == set(self.files)
        assert {entry.size for entry in conflict.entries} == {4}

    def test_case_insensitive_collision_with_identical_contents(self):
        files = {
            "fancy/Module.py": b"same\n",
            "fancy/module.py": b"same\n",
        }

        sensitive = build(files, CaseSensitivity.CASE_SENSITIVE)
        insensitive = build(files, CaseSensitivity.CASE_INSENSITIVE)

        assert sensitive.conflicts == ()
        (conflict,) = insensitive.conflicts
        assert conflict.identical is True
        assert insensitive.content_conflicts == ()

    def test_folding_uses_casefold_not_lower(self):
        # "ß".casefold() == "ss", while "ß".lower() == "ß".
        files = {
            "fancy/Straße.py": b"a",
            "fancy/strasse.py": b"b",
        }

        sensitive = build(files, CaseSensitivity.CASE_SENSITIVE)
        insensitive = build(files, CaseSensitivity.CASE_INSENSITIVE)

        assert sensitive.conflicts == ()
        assert len(insensitive.conflicts) == 1
        assert insensitive.conflicts[0].identical is False

    def test_two_platform_plans_disagree_on_same_wheel(self):
        source = FakeWheelSource(regular_files=self.files)
        sensitive = build_install_plan(source, CaseSensitivity.CASE_SENSITIVE)
        insensitive = build_install_plan(source, CaseSensitivity.CASE_INSENSITIVE)

        assert sensitive.content_conflicts == ()
        assert len(insensitive.content_conflicts) == 1
        # The member-to-scheme mapping itself is platform independent.
        assert [entry.target_path for entry in sensitive.entries] == [
            entry.target_path for entry in insensitive.entries
        ]


def test_invalid_data_scheme_is_rejected_like_install():
    from installer.exceptions import InvalidWheelSource

    source = FakeWheelSource(regular_files={"fancy-1.0.0.data/bogus/fancy/x.py": b"x"})
    with pytest.raises(InvalidWheelSource):
        build_install_plan(source, CaseSensitivity.CASE_SENSITIVE)
