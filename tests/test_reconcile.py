import io
import json
import zipfile
from pathlib import Path

import pytest

from installer import install
from installer.destinations import SchemeDictionaryDestination
from installer.reconcile import (
    Provenance,
    Status,
    reconcile_wheel,
)
from installer.scripts import Script
from installer.sources import WheelFile
from installer.utils import SCHEME_NAMES


def _make_wheel(tmp_path, files, name="fancy"):
    path = tmp_path / f"{name}-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        for filename, content in files.items():
            archive.writestr(filename, content)
    return path


def _wheel_files(name="fancy", *, scripts=None, entry_points=None):
    scripts = scripts or {}
    files = {
        f"{name}/__init__.py": b"def main():\n    print('fancy')\n",
        f"{name}-1.0.0.dist-info/top_level.txt": f"{name}\n".encode(),
        f"{name}-1.0.0.dist-info/WHEEL": (
            b"Wheel-Version: 1.0\n"
            b"Generator: tests\n"
            b"Root-Is-Purelib: true\n"
            b"Tag: py3-none-any\n"
        ),
        f"{name}-1.0.0.dist-info/METADATA": (
            f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0.0\n".encode()
        ),
    }
    for script_name, data in scripts.items():
        files[f"{name}-1.0.0.data/scripts/{script_name}"] = data
    if entry_points is not None:
        files[f"{name}-1.0.0.dist-info/entry_points.txt"] = entry_points
    files[f"{name}-1.0.0.dist-info/RECORD"] = b""
    return files


def _write_wheel(tmp_path, *, scripts=None, entry_points=None, name="fancy"):
    files = _wheel_files(name, scripts=scripts, entry_points=entry_points)
    # The wheel reader expects a RECORD, and tolerates an empty one because the
    # files are not validated unless explicitly requested.
    return _make_wheel(tmp_path, files, name=name)


@pytest.fixture()
def scheme_dict(tmp_path):
    result = {}
    for scheme in SCHEME_NAMES:
        path = tmp_path / "install" / scheme
        path.mkdir(parents=True, exist_ok=True)
        result[scheme] = str(path)
    return result


def _install(wheel, scheme_dict, *, interpreter="/my/python", kind="posix"):
    destination = SchemeDictionaryDestination(
        scheme_dict,
        interpreter=interpreter,
        script_kind=kind,
        bytecode_optimization_levels=(),
    )
    with WheelFile.open(wheel) as source:
        install(source, destination, {})


def _entry(key):
    return f"[console_scripts]\n{key} = fancy:main\n"


class TestReconcileCleanInstall:
    def test_classifies_verbatim_rewritten_and_generated(self, tmp_path, scheme_dict):
        wheel = _write_wheel(
            tmp_path,
            scripts={"plain-script": b"#!python\nprint('hello')\n"},
            entry_points=_entry("fancy-cli").encode(),
        )
        _install(wheel, scheme_dict)

        ledger = reconcile_wheel(
            wheel,
            scheme_dict=scheme_dict,
            interpreter="/my/python",
            script_kind="posix",
        )

        assert ledger.ok, ledger.to_json()
        entries = {(str(entry.scheme), entry.path): entry for entry in ledger.entries}
        assert (
            entries[("purelib", "fancy/__init__.py")].provenance is Provenance.VERBATIM
        )
        assert entries[("scripts", "plain-script")].provenance is Provenance.REWRITTEN
        assert entries[("scripts", "fancy-cli")].provenance is Provenance.GENERATED
        assert (
            entries[("purelib", "fancy-1.0.0.dist-info/RECORD")].provenance
            is Provenance.GENERATED
        )

    def test_records_both_side_digests(self, tmp_path, scheme_dict):
        wheel = _write_wheel(
            tmp_path, scripts={"plain-script": b"#!python\nprint('hello')\n"}
        )
        _install(wheel, scheme_dict)

        ledger = reconcile_wheel(
            wheel,
            scheme_dict=scheme_dict,
            interpreter="/my/python",
            script_kind="posix",
        )
        entry = next(
            item
            for item in ledger.entries
            if str(item.scheme) == "scripts" and item.path == "plain-script"
        )

        assert entry.status is Status.MATCH
        assert entry.source is not None
        assert entry.expected is not None
        assert entry.installed is not None
        assert len(entry.source.sha256) == 64
        assert len(entry.expected.sha256) == 64
        assert len(entry.installed.sha256) == 64
        assert entry.source.sha256 != entry.expected.sha256
        assert entry.expected.sha256 == entry.installed.sha256

    def test_windows_launcher_timestamps_do_not_cause_difference(
        self, tmp_path, scheme_dict
    ):
        wheel = _write_wheel(tmp_path, entry_points=_entry("fancy-cli").encode())
        _install(
            wheel, scheme_dict, interpreter="C:/Python/python.exe", kind="win-amd64"
        )

        script = Path(scheme_dict["scripts"]) / "fancy-cli.exe"
        launcher = Script("fancy-cli", "fancy", "main", "console")._get_launcher_data(
            "win-amd64"
        )
        assert launcher is not None
        installed = script.read_bytes()
        rest = installed[len(launcher) :]
        shebang, _, archive_bytes = rest.partition(b"\n")
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as old_archive:
            code = old_archive.read("__main__.py")
        new_archive = io.BytesIO()
        with zipfile.ZipFile(new_archive, "w") as archive:
            archive.writestr(
                zipfile.ZipInfo("__main__.py", date_time=(2001, 2, 3, 4, 5, 6)),
                code,
            )
        script.write_bytes(launcher + shebang + b"\n" + new_archive.getvalue())

        ledger = reconcile_wheel(
            wheel,
            scheme_dict=scheme_dict,
            interpreter="C:/Python/python.exe",
            script_kind="win-amd64",
        )

        assert ledger.ok, ledger.to_json()
        entry = next(item for item in ledger.entries if item.path == "fancy-cli.exe")
        assert entry.provenance is Provenance.GENERATED
        assert entry.status is Status.MATCH
        assert entry.expected.sha256 != entry.installed.sha256
        assert "ZIP timestamps differ" in entry.detail
        record = next(item for item in ledger.entries if item.path.endswith("RECORD"))
        assert record.status is Status.MATCH
        assert record.expected is not None
        assert record.installed is not None

    def test_windows_launcher_payload_change_is_an_unexpected_difference(
        self, tmp_path, scheme_dict
    ):
        wheel = _write_wheel(tmp_path, entry_points=_entry("fancy-cli").encode())
        _install(
            wheel, scheme_dict, interpreter="C:/Python/python.exe", kind="win-amd64"
        )

        script = Path(scheme_dict["scripts"]) / "fancy-cli.exe"
        launcher = Script("fancy-cli", "fancy", "main", "console")._get_launcher_data(
            "win-amd64"
        )
        assert launcher is not None
        installed = script.read_bytes()
        rest = installed[len(launcher) :]
        shebang, _, archive_bytes = rest.partition(b"\n")
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            code = archive.read("__main__.py")
        corrupted = code.replace(b"fancy", b"faNcy")
        new_archive = io.BytesIO()
        with zipfile.ZipFile(new_archive, "w") as archive:
            archive.writestr("__main__.py", corrupted)
        script.write_bytes(launcher + shebang + b"\n" + new_archive.getvalue())

        ledger = reconcile_wheel(
            wheel,
            scheme_dict=scheme_dict,
            interpreter="C:/Python/python.exe",
            script_kind="win-amd64",
        )

        entry = next(item for item in ledger.entries if item.path == "fancy-cli.exe")
        assert entry.status is Status.UNEXPECTED_DIFFERENCE
        assert entry.difference is not None
        assert entry.difference.expected_byte is not None
        assert entry.difference.installed_byte is not None

    def test_windows_record_extra_row_is_an_unexpected_difference(
        self, tmp_path, scheme_dict
    ):
        wheel = _write_wheel(tmp_path, entry_points=_entry("fancy-cli").encode())
        _install(
            wheel, scheme_dict, interpreter="C:/Python/python.exe", kind="win-amd64"
        )
        record = Path(scheme_dict["purelib"]) / "fancy-1.0.0.dist-info" / "RECORD"
        record.write_bytes(record.read_bytes() + b"evil,,\n")

        ledger = reconcile_wheel(
            wheel,
            scheme_dict=scheme_dict,
            interpreter="C:/Python/python.exe",
            script_kind="win-amd64",
        )

        entry = next(item for item in ledger.entries if item.path.endswith("RECORD"))
        assert entry.status is Status.UNEXPECTED_DIFFERENCE
        assert entry.difference is not None


class TestReconcileScriptTampering:
    def test_single_body_byte_change_is_an_unexpected_difference(
        self, tmp_path, scheme_dict
    ):
        wheel = _write_wheel(
            tmp_path,
            scripts={"plain-script": b"#!python\nprint('hello')\n# keep this body\n"},
        )
        _install(wheel, scheme_dict)
        script = Path(scheme_dict["scripts"]) / "plain-script"
        original = script.read_bytes()
        body_offset = original.index(b"hello")
        script.write_bytes(original[:body_offset] + b"j" + original[body_offset + 1 :])

        ledger = reconcile_wheel(
            wheel,
            scheme_dict=scheme_dict,
            interpreter="/my/python",
            script_kind="posix",
        )

        assert not ledger.ok
        entry = next(item for item in ledger.entries if item.path == "plain-script")
        assert entry.provenance is Provenance.REWRITTEN
        assert entry.status is Status.UNEXPECTED_DIFFERENCE
        assert entry.difference is not None
        assert entry.difference.line == 2
        assert entry.difference.offset == body_offset
        assert entry.difference.expected_byte == ord("h")
        assert entry.difference.installed_byte == ord("j")
        assert entry.expected is not None
        assert entry.installed is not None
        assert entry.expected.sha256 != entry.installed.sha256

    def test_extra_script_file_is_reported_as_unknown(self, tmp_path, scheme_dict):
        wheel = _write_wheel(
            tmp_path, scripts={"plain-script": b"#!python\nprint('hello')\n"}
        )
        _install(wheel, scheme_dict)
        extra = Path(scheme_dict["scripts"]) / "unexpected-tool"
        extra.write_text("#!/bin/sh\necho unexpected\n", encoding="utf-8")

        ledger = reconcile_wheel(
            wheel,
            scheme_dict=scheme_dict,
            interpreter="/my/python",
            script_kind="posix",
        )

        entry = next(item for item in ledger.entries if item.path == "unexpected-tool")
        assert entry.provenance is Provenance.UNKNOWN
        assert entry.status is Status.UNKNOWN
        assert entry.source is None
        assert entry.expected is None
        assert entry.installed is not None
        assert entry.installed.size == extra.stat().st_size

    def test_extra_script_nested_in_subdirectory_is_reported(
        self, tmp_path, scheme_dict
    ):
        wheel = _write_wheel(
            tmp_path, scripts={"nested/plain-script": b"#!python\nprint('hello')\n"}
        )
        _install(wheel, scheme_dict)
        extra = Path(scheme_dict["scripts"]) / "nested" / "unexpected-tool"
        extra.write_text("#!/bin/sh\necho unexpected\n", encoding="utf-8")

        ledger = reconcile_wheel(
            wheel,
            scheme_dict=scheme_dict,
            interpreter="/my/python",
            script_kind="posix",
        )

        entry = next(
            item for item in ledger.entries if item.path == "nested/unexpected-tool"
        )
        assert entry.status is Status.UNKNOWN

    def test_unknown_files_are_not_accepted_for_the_whole_scripts_directory(
        self, tmp_path, scheme_dict
    ):
        wheel = _write_wheel(
            tmp_path, scripts={"plain-script": b"#!python\nprint('hello')\n"}
        )
        _install(wheel, scheme_dict)
        for name in ("generated-a", "generated-b"):
            (Path(scheme_dict["scripts"]) / name).write_text("x", encoding="utf-8")

        ledger = reconcile_wheel(
            wheel,
            scheme_dict=scheme_dict,
            interpreter="/my/python",
            script_kind="posix",
        )

        unknown = {
            entry.path
            for entry in ledger.entries
            if entry.provenance is Provenance.UNKNOWN
        }
        assert unknown == {"generated-a", "generated-b"}

    def test_script_without_python_shebang_is_verbatim(self, tmp_path, scheme_dict):
        wheel = _write_wheel(
            tmp_path, scripts={"plain-script": b"#!/bin/sh\necho hello\n"}
        )
        _install(wheel, scheme_dict)

        ledger = reconcile_wheel(
            wheel,
            scheme_dict=scheme_dict,
            interpreter="/my/python",
            script_kind="posix",
        )

        entry = next(item for item in ledger.entries if item.path == "plain-script")
        assert entry.provenance is Provenance.VERBATIM
        assert entry.status is Status.MATCH

    def test_tampered_verbatim_script_is_reported(self, tmp_path, scheme_dict):
        wheel = _write_wheel(
            tmp_path, scripts={"plain-script": b"#!/bin/sh\necho hello\n"}
        )
        _install(wheel, scheme_dict)
        script = Path(scheme_dict["scripts"]) / "plain-script"
        script.write_bytes(script.read_bytes().replace(b"hello", b"jello"))

        ledger = reconcile_wheel(
            wheel,
            scheme_dict=scheme_dict,
            interpreter="/my/python",
            script_kind="posix",
        )

        entry = next(item for item in ledger.entries if item.path == "plain-script")
        assert entry.provenance is Provenance.VERBATIM
        assert entry.status is Status.UNEXPECTED_DIFFERENCE
        assert entry.difference is not None
        assert entry.difference.line == 2


class TestReconcileMissingAndRecord:
    def test_missing_installed_file_is_reported(self, tmp_path, scheme_dict):
        wheel = _write_wheel(
            tmp_path, scripts={"plain-script": b"#!python\nprint('hello')\n"}
        )
        _install(wheel, scheme_dict)
        (Path(scheme_dict["scripts"]) / "plain-script").unlink()

        ledger = reconcile_wheel(
            wheel,
            scheme_dict=scheme_dict,
            interpreter="/my/python",
            script_kind="posix",
        )

        entry = next(item for item in ledger.entries if item.path == "plain-script")
        assert entry.status is Status.MISSING
        assert entry.expected is not None
        assert entry.installed is None

    def test_tampered_record_is_reported(self, tmp_path, scheme_dict):
        wheel = _write_wheel(tmp_path)
        _install(wheel, scheme_dict)
        record = Path(scheme_dict["purelib"]) / "fancy-1.0.0.dist-info" / "RECORD"
        record.write_bytes(record.read_bytes() + b"added-by-someone,,\n")

        ledger = reconcile_wheel(
            wheel,
            scheme_dict=scheme_dict,
            interpreter="/my/python",
            script_kind="posix",
        )

        entry = next(item for item in ledger.entries if item.path.endswith("/RECORD"))
        assert entry.provenance is Provenance.GENERATED
        assert entry.status is Status.UNEXPECTED_DIFFERENCE
        assert entry.expected is not None
        assert entry.installed is not None
        assert entry.expected.sha256 != entry.installed.sha256


class TestReconcileDestdir:
    def test_destdir_is_applied_to_both_sides(self, tmp_path):
        wheel = _write_wheel(
            tmp_path, scripts={"plain-script": b"#!python\nprint('hello')\n"}
        )
        prefix = tmp_path / "prefix"
        scheme_dict = {}
        for scheme in SCHEME_NAMES:
            path = prefix / scheme
            path.mkdir(parents=True, exist_ok=True)
            scheme_dict[scheme] = str(path)
        destdir = tmp_path / "staging"
        _install(
            wheel,
            {key: str(prefix / key) for key in scheme_dict},
            interpreter="/my/python",
        )
        # Move the theoretical prefix into the staging root, as --destdir does.
        for scheme in SCHEME_NAMES:
            source = prefix / scheme
            target = destdir / str(source.relative_to(source.anchor))
            target.parent.mkdir(parents=True, exist_ok=True)
            source.rename(target)

        ledger = reconcile_wheel(
            wheel,
            scheme_dict=scheme_dict,
            interpreter="/my/python",
            script_kind="posix",
            destdir=destdir,
        )

        assert ledger.ok, ledger.to_json()


class TestReconcileCli:
    def test_json_output_and_exit_status(self, tmp_path, scheme_dict, capsys):
        wheel = _write_wheel(
            tmp_path, scripts={"plain-script": b"#!python\nprint('hello')\n"}
        )
        _install(wheel, scheme_dict)
        (Path(scheme_dict["scripts"]) / "extra").write_text("x", encoding="utf-8")

        from installer.reconcile import _main

        exit_code = _main(
            [
                str(wheel),
                *[
                    argument
                    for scheme, path in scheme_dict.items()
                    for argument in ("--scheme", f"{scheme}={path}")
                ],
                "--interpreter",
                "/my/python",
                "--script-kind",
                "posix",
                "--json",
            ]
        )

        assert exit_code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        statuses = {
            (entry["scheme"], entry["path"]): entry["status"]
            for entry in payload["entries"]
        }
        assert statuses[("scripts", "extra")] == "unknown"


def test_ledger_json_is_stable_and_auditable(tmp_path, scheme_dict):
    wheel = _write_wheel(
        tmp_path,
        scripts={"plain-script": b"#!python\nprint('hello')\n"},
        entry_points=_entry("fancy-cli").encode(),
    )
    _install(wheel, scheme_dict)

    ledger = reconcile_wheel(
        wheel,
        scheme_dict=scheme_dict,
        interpreter="/my/python",
        script_kind="posix",
    )

    payload = json.loads(ledger.to_json())
    assert payload["counts"]["provenance"] == {
        "verbatim": payload["counts"]["provenance"]["verbatim"],
        "rewritten": 1,
        "generated": 2,
        "unknown": 0,
    }
    assert payload["counts"]["status"]["match"] == len(payload["entries"])


class TestReconcileConflicts:
    def test_duplicate_wheel_member_is_recorded_as_conflict(
        self, tmp_path, scheme_dict
    ):
        wheel = _write_wheel(tmp_path, scripts={"dup": b"#!python\nprint('first')\n"})
        with (
            pytest.warns(UserWarning, match="Duplicate name"),
            zipfile.ZipFile(wheel, "a") as archive,
        ):
            archive.writestr(
                "fancy-1.0.0.data/scripts/dup", b"#!python\nprint('second')\n"
            )
        destination = SchemeDictionaryDestination(
            scheme_dict,
            interpreter="/my/python",
            script_kind="posix",
            bytecode_optimization_levels=(),
            overwrite_existing=True,
        )
        with WheelFile.open(wheel) as source:
            install(source, destination, {})

        ledger = reconcile_wheel(
            wheel,
            scheme_dict=scheme_dict,
            interpreter="/my/python",
            script_kind="posix",
        )

        entries = [item for item in ledger.entries if item.path == "dup"]
        assert [item.status for item in entries] == [
            Status.CONFLICT,
            Status.MATCH,
        ]
        conflict = entries[0]
        assert conflict.provenance is Provenance.REWRITTEN
        assert conflict.source is not None
        assert conflict.expected is not None
        assert conflict.installed is not None


class TestReconcileScanScope:
    def test_non_scanned_scheme_ignores_unrelated_package_files(
        self, tmp_path, scheme_dict
    ):
        wheel = _write_wheel(tmp_path)
        _install(wheel, scheme_dict)
        other = Path(scheme_dict["purelib"]) / "other" / "__init__.py"
        other.parent.mkdir(parents=True)
        other.write_text("", encoding="utf-8")

        ledger = reconcile_wheel(
            wheel,
            scheme_dict=scheme_dict,
            interpreter="/my/python",
            script_kind="posix",
        )

        assert ledger.ok, ledger.to_json()

    def test_scanned_purelib_reports_unrelated_package_files(
        self, tmp_path, scheme_dict
    ):
        wheel = _write_wheel(tmp_path)
        _install(wheel, scheme_dict)
        other = Path(scheme_dict["purelib"]) / "other" / "__init__.py"
        other.parent.mkdir(parents=True)
        other.write_text("", encoding="utf-8")

        ledger = reconcile_wheel(
            wheel,
            scheme_dict=scheme_dict,
            interpreter="/my/python",
            script_kind="posix",
            scan_schemes=("scripts", "purelib"),
        )

        entry = next(
            item for item in ledger.entries if item.path == "other/__init__.py"
        )
        assert entry.scheme == "purelib"
        assert entry.status is Status.UNKNOWN

    def test_pyc_in_scripts_is_not_silently_ignored(self, tmp_path, scheme_dict):
        wheel = _write_wheel(
            tmp_path, scripts={"plain-script": b"#!python\nprint('hello')\n"}
        )
        _install(wheel, scheme_dict)
        bytecode = Path(scheme_dict["scripts"]) / "stray.pyc"
        bytecode.write_bytes(b"not real bytecode")

        ledger = reconcile_wheel(
            wheel,
            scheme_dict=scheme_dict,
            interpreter="/my/python",
            script_kind="posix",
        )

        entry = next(item for item in ledger.entries if item.path == "stray.pyc")
        assert entry.status is Status.UNKNOWN
