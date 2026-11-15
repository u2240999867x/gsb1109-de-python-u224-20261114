"""Tests for installer.catalog"""

import textwrap

import pytest

from installer.catalog import (
    CONSOLE_GROUP,
    GUI_GROUP,
    AmbiguousCommand,
    CommandCatalog,
    CommandNotFound,
    CommandProvider,
    InvalidCommand,
    QueryResult,
    Release,
    build_catalog,
    default_environment,
    evaluate_marker,
    normalize_group,
    parse_entry_points,
    parse_entry_points_file,
)

LINUX_ENVIRONMENT = {
    "python_version": "3.10",
    "python_full_version": "3.10.12",
    "sys_platform": "linux",
    "os_name": "posix",
    "platform_system": "Linux",
}

WINDOWS_ENVIRONMENT = {
    "python_version": "3.12",
    "python_full_version": "3.12.4",
    "sys_platform": "win32",
    "os_name": "nt",
    "platform_system": "Windows",
}

MODERN_ENVIRONMENT = {
    "python_version": "3.13",
    "python_full_version": "3.13.2",
    "sys_platform": "linux",
    "os_name": "posix",
    "platform_system": "Linux",
}


def text(value):
    return textwrap.dedent(value)


class TestRelease:
    def test_canonicalizes_identity(self):
        release = Release("My_Package", "1.0")
        assert release.distribution == "my-package"
        assert release.version == "1.0"
        assert release.key == ("my-package", "1.0")
        assert str(release) == "my-package==1.0"

    def test_release_equality_uses_identity(self):
        assert Release("My_Package", "1.0") == Release("my-package", "1.0")
        assert Release("my-package", "1.0") != Release("my-package", "1.1")


class TestParseEntryPoints:
    def test_records_release_group_name_and_conditions(self):
        entries = parse_entry_points(
            text(
                """
                [console_scripts]
                demo = demo.cli:main
                fancy = demo.fancy:main [extra-a,extra-b] ; python_version >= "3.10"

                [gui_scripts]
                demo = demo.gui:main
                """
            ),
            "Demo_Package",
            "2.1",
        )

        assert entries == (
            CommandProvider(
                "demo-package", "2.1", "console_scripts", "demo", "demo.cli", "main"
            ),
            CommandProvider(
                "demo-package",
                "2.1",
                "console_scripts",
                "fancy",
                "demo.fancy",
                "main",
                extras=("extra-a", "extra-b"),
                marker='python_version >= "3.10"',
            ),
            CommandProvider(
                "demo-package", "2.1", "gui_scripts", "demo", "demo.gui", "main"
            ),
        )

    def test_keeps_gui_and_console_with_same_name(self):
        entries = parse_entry_points(
            text(
                """
                [console_scripts]
                demo = demo.cli:main

                [gui_scripts]
                demo = demo.gui:main
                """
            ),
            "demo",
            "1.0",
        )
        assert [entry.group for entry in entries] == [
            "console_scripts",
            "gui_scripts",
        ]
        assert len(entries) == 2

    def test_group_aliases_are_normalized(self):
        entries = parse_entry_points(
            text(
                """
                [console]
                demo = demo:main

                [gui]
                demo-gui = demo:gui
                """
            )
        )
        assert [entry.group for entry in entries] == [
            "console_scripts",
            "gui_scripts",
        ]

    def test_extra_groups_are_preserved(self):
        entries = parse_entry_points(
            text(
                """
                [demo.plugins]
                plugin = demo.plugin:Plugin
                """
            )
        )
        assert entries[0].group == "demo.plugins"
        assert entries[0].entry_point == "demo.plugin:Plugin"

    def test_script_group_requires_callable(self):
        with pytest.raises(InvalidCommand):
            parse_entry_points(
                text(
                    """
                    [console_scripts]
                    demo = demo.cli
                    """
                )
            )

    def test_invalid_entry_point_is_rejected(self):
        with pytest.raises(InvalidCommand):
            parse_entry_points(
                text(
                    """
                    [console_scripts]
                    demo = not a valid entry point!
                    """
                )
            )

    def test_invalid_marker_is_rejected(self):
        with pytest.raises(InvalidCommand):
            parse_entry_points(
                text(
                    """
                    [console_scripts]
                    demo = demo:main ; unknown_variable == "x"
                    """
                )
            )

    def test_filter_by_group(self):
        entries = parse_entry_points(
            text(
                """
                [console_scripts]
                demo = demo:main

                [gui_scripts]
                demo-gui = demo:gui
                """
            ),
            groups=["gui"],
        )
        assert [entry.name for entry in entries] == ["demo-gui"]

    def test_parse_file(self, tmp_path):
        path = tmp_path / "entry_points.txt"
        path.write_text(
            text(
                """
                [console_scripts]
                demo = demo:main
                """
            ),
            encoding="utf-8",
        )
        (entry,) = parse_entry_points_file(path, "demo", "1.0")
        assert entry.release.key == ("demo", "1.0")
        assert entry.name == "demo"

    def test_provider_helpers(self):
        (entry,) = parse_entry_points(
            text(
                """
                [gui_scripts]
                demo = demo:main
                """
            ),
            "demo",
            "1.0",
        )
        assert entry.section == "gui"
        assert entry.entry_point == "demo:main"
        assert entry.condition == ""
        assert entry.release == Release("demo", "1.0")

    def test_row_round_trip(self):
        (entry,) = parse_entry_points(
            text(
                """
                [console_scripts]
                demo = demo:main ; python_version >= "3.10"
                """
            ),
            "demo",
            "1.0",
        )
        assert CommandProvider.from_row(entry.as_row()) == entry


class TestEvaluateMarker:
    @pytest.mark.parametrize(
        ("marker", "environment", "expected"),
        [
            ('python_version >= "3.10"', {"python_version": "3.10"}, True),
            ('python_version > "3.10"', {"python_version": "3.10"}, False),
            ('python_version < "3.12"', {"python_version": "3.12"}, False),
            ('python_version == "3.10.*"', {"python_version": "3.10.4"}, True),
            ('python_version ~= "3.10"', {"python_version": "3.11"}, True),
            (
                'python_version in "3.9 3.10"',
                {"python_version": "3.10"},
                True,
            ),
            (
                'python_version not in "3.9 3.10"',
                {"python_version": "3.10"},
                False,
            ),
            ('sys_platform == "win32"', {"sys_platform": "linux"}, False),
            ('os_name != "nt"', {"os_name": "posix"}, True),
            (
                '(python_version >= "3.9" and python_version < "3.12") '
                'or sys_platform == "win32"',
                {"python_version": "3.12", "sys_platform": "win32"},
                True,
            ),
            ('extra == "dev"', {"extra": "dev"}, True),
            ('extra == "dev"', {"extra": ""}, False),
        ],
    )
    def test_evaluates(self, marker, environment, expected):
        assert evaluate_marker(marker, environment) is expected

    def test_uses_current_environment_by_default(self):
        environment = default_environment()
        marker = f'python_version >= "{environment["python_version"]}"'
        assert evaluate_marker(marker) is True

    @pytest.mark.parametrize(
        "marker",
        [
            "python_version >",
            'python_version > "3.10',
            "(python_version > '3.10'",
            "unknown_variable == 'x'",
            'python_version > "3.10" and',
        ],
    )
    def test_invalid_markers(self, marker):
        with pytest.raises(InvalidCommand):
            evaluate_marker(marker, LINUX_ENVIRONMENT)


class TestNormalizeGroup:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("console", CONSOLE_GROUP),
            ("gui", GUI_GROUP),
            ("console_scripts", CONSOLE_GROUP),
            ("gui_scripts", GUI_GROUP),
            ("demo.plugins", "demo.plugins"),
        ],
    )
    def test_normalizes(self, value, expected):
        assert normalize_group(value) == expected


class TestReleaseScopedStorage:
    @pytest.fixture
    def catalog(self):
        catalog = CommandCatalog()
        catalog.add_release(
            "Demo_Package",
            "1.0",
            text(
                """
                [console_scripts]
                demo = demo.cli:main
                legacy = demo.legacy:main
                """
            ),
        )
        catalog.add_release(
            "demo-package",
            "2.0",
            text(
                """
                [console_scripts]
                demo = demo.cli:main
                """
            ),
        )
        return catalog

    def test_releases_are_recorded_by_identity_and_version(self, catalog):
        assert catalog.releases == (
            Release("demo-package", "1.0"),
            Release("demo-package", "2.0"),
        )
        assert catalog.has_release("DEMO.package", "2.0")
        assert not catalog.has_release("demo-package", "3.0")
        assert catalog.release_versions("demo-package") == ("1.0", "2.0")

    def test_entries_carry_release_identity(self, catalog):
        entries = catalog.entries(releases=[("demo-package", "1.0")])
        assert {(entry.name, entry.version) for entry in entries} == {
            ("demo", "1.0"),
            ("legacy", "1.0"),
        }

    def test_entry_only_in_old_release_is_not_carried_forward(self, catalog):
        assert catalog.find_providers("legacy", releases=["demo-package==1.0"])
        assert catalog.find_providers("legacy", releases=["demo-package==2.0"]) == ()

    def test_registering_a_release_replaces_previous_contents(self, catalog):
        catalog.add_release(
            "demo-package",
            "2.0",
            text(
                """
                [gui_scripts]
                demo = demo.gui:main
                """
            ),
        )
        assert catalog.find_providers("demo", releases=["demo-package==2.0"])[
            0
        ].group == (GUI_GROUP)
        assert catalog.find_providers("demo", releases=["demo-package==1.0"])[
            0
        ].group == (CONSOLE_GROUP)

    def test_remove_release(self, catalog):
        assert catalog.remove_release("Demo_Package", "1.0") == 2
        assert not catalog.has_release("demo-package", "1.0")
        assert catalog.release_versions("demo-package") == ("2.0",)

    def test_latest_release_uses_numeric_ordering(self):
        catalog = CommandCatalog()
        for version in ("1.9", "1.10", "1.2"):
            catalog.add_release("demo", version, "")
        assert catalog.latest_release("demo") == Release("demo", "1.10")


class TestReleaseSelection:
    @pytest.fixture
    def catalog(self):
        catalog = CommandCatalog()
        catalog.add_release("alpha", "1.0", "[console_scripts]\nalpha = alpha:main\n")
        catalog.add_release("alpha", "2.0", "[console_scripts]\nalpha = alpha:main\n")
        catalog.add_release("beta", "1.0", "[console_scripts]\nbeta = beta:main\n")
        return catalog

    @pytest.mark.parametrize(
        ("selection", "expected"),
        [
            (None, {("alpha", "1.0"), ("alpha", "2.0"), ("beta", "1.0")}),
            (["alpha==1.0"], {("alpha", "1.0")}),
            ([("alpha", "2.0")], {("alpha", "2.0")}),
            ([Release("alpha", "1.0")], {("alpha", "1.0")}),
            (["alpha"], {("alpha", "1.0"), ("alpha", "2.0")}),
            ({"alpha": ["2.0"], "beta": None}, {("alpha", "2.0"), ("beta", "1.0")}),
        ],
    )
    def test_selection_forms(self, catalog, selection, expected):
        releases = {
            (entry.distribution, entry.version)
            for entry in catalog.entries(releases=selection)
        }
        assert releases == expected

    def test_unselected_release_is_not_reported(self, catalog):
        assert catalog.find_providers("beta", releases=["alpha"]) == ()
        assert (
            catalog.find_providers("alpha", releases=["alpha==1.0"])[0].version == "1.0"
        )


class TestEnvironmentConditions:
    @pytest.fixture
    def catalog(self):
        catalog = CommandCatalog()
        catalog.add_release(
            "demo",
            "1.0",
            text(
                """
                [console_scripts]
                always = demo:always
                modern = demo:modern ; python_version >= "3.12"
                windows = demo:windows ; sys_platform == "win32"
                dev = demo:dev [development]
                extra-marker = demo:extra ; extra == "docs"
                """
            ),
        )
        return catalog

    def test_marker_true_for_environment(self, catalog):
        names = [
            entry.name for entry in catalog.search(environment=WINDOWS_ENVIRONMENT)
        ]
        assert "windows" in names
        assert "modern" in names
        assert "always" in names

    def test_marker_false_for_environment(self, catalog):
        names = [entry.name for entry in catalog.search(environment=LINUX_ENVIRONMENT)]
        assert "windows" not in names
        assert "modern" not in names

    def test_extras_condition(self, catalog):
        assert catalog.find_providers("dev") == ()
        assert catalog.find_providers("dev", extras=["development"])
        assert catalog.find_providers("extra-marker", extras=["docs"])

    def test_include_inactive_returns_declared_entries(self, catalog):
        all_entries = catalog.search(include_inactive=True)
        assert len(all_entries) == 5
        active_entries = catalog.search(environment=LINUX_ENVIRONMENT)
        assert len(active_entries) < len(all_entries)

    def test_environment_extras_key(self, catalog):
        providers = catalog.find_providers("dev", environment={"extras": "development"})
        assert len(providers) == 1


class TestCoexistenceAndAmbiguity:
    def test_gui_and_console_entries_with_same_name_coexist(self):
        catalog = build_catalog(
            [
                (
                    "demo",
                    "1.0",
                    text(
                        """
                        [console_scripts]
                        demo = demo.cli:main

                        [gui_scripts]
                        demo = demo.gui:main
                        """
                    ),
                )
            ]
        )

        result = catalog.query("demo")
        assert len(result) == 2
        assert result.ambiguous is False
        assert set(result.groups) == {CONSOLE_GROUP, GUI_GROUP}
        assert result.unique("console").entry_point == "demo.cli:main"
        assert result.unique("gui").entry_point == "demo.gui:main"

    def test_two_console_providers_are_ambiguous(self):
        catalog = build_catalog(
            [
                ("alpha", "1.0", "[console_scripts]\ndemo = alpha:main\n"),
                ("beta", "1.0", "[console_scripts]\ndemo = beta:main\n"),
            ]
        )

        result = catalog.query("demo", group=CONSOLE_GROUP)
        assert result.ambiguous is True
        assert len(result) == 2
        assert set(result.ambiguities) == {CONSOLE_GROUP}

        with pytest.raises(AmbiguousCommand) as info:
            catalog.resolve("demo")

        assert {provider.distribution for provider in info.value.providers} == {
            "alpha",
            "beta",
        }
        assert info.value.group == CONSOLE_GROUP

    def test_selected_release_set_controls_ambiguity(self):
        catalog = build_catalog(
            [
                ("alpha", "1.0", "[console_scripts]\ndemo = alpha:main\n"),
                ("beta", "1.0", "[console_scripts]\ndemo = beta:main\n"),
            ]
        )

        assert catalog.resolve("demo", releases=["alpha"]).distribution == "alpha"
        assert catalog.resolve("demo", releases=["beta"]).distribution == "beta"
        with pytest.raises(AmbiguousCommand):
            catalog.resolve("demo", releases=["alpha", "beta"])

    def test_ambiguity_ignores_inactive_providers(self):
        catalog = build_catalog(
            [
                (
                    "alpha",
                    "1.0",
                    '[console_scripts]\ndemo = alpha:main ; sys_platform == "win32"\n',
                ),
                ("beta", "1.0", "[console_scripts]\ndemo = beta:main\n"),
            ]
        )

        assert catalog.query("demo", environment=LINUX_ENVIRONMENT).ambiguous is False
        assert (
            catalog.resolve("demo", environment=LINUX_ENVIRONMENT).distribution
            == "beta"
        )
        assert catalog.query("demo", environment=WINDOWS_ENVIRONMENT).ambiguous is True

    def test_ambiguities_across_catalog(self):
        catalog = build_catalog(
            [
                ("alpha", "1.0", "[console_scripts]\ndemo = alpha:main\n"),
                (
                    "beta",
                    "1.0",
                    """
                    [console_scripts]
                    demo = beta:main

                    [gui_scripts]
                    demo = beta:gui
                    """,
                ),
            ]
        )

        assert set(catalog.ambiguities()) == {("demo", CONSOLE_GROUP)}

    def test_missing_provider(self):
        catalog = build_catalog(
            [("alpha", "1.0", "[console_scripts]\ndemo = alpha:main\n")]
        )
        with pytest.raises(CommandNotFound):
            catalog.resolve("missing")
        with pytest.raises(CommandNotFound):
            catalog.resolve("demo", group=GUI_GROUP)
        assert not catalog.query("missing")


class TestCatalogIngestion:
    def test_add_source_and_wheel(self, fancy_wheel):
        from installer.sources import WheelFile

        with WheelFile.open(fancy_wheel) as source:
            from_source = CommandCatalog()
            from_source.add_source(source)

        from_wheel = CommandCatalog()
        from_wheel.add_wheel(fancy_wheel)

        assert {(entry.name, entry.group) for entry in from_source.entries()} == {
            ("fancy", CONSOLE_GROUP),
            ("fancy-gui", GUI_GROUP),
        }
        assert [entry.as_row() for entry in from_source.entries()] == [
            entry.as_row() for entry in from_wheel.entries()
        ]

    def test_add_entries_and_alias_ingest(self):
        provider = CommandProvider("demo", "1.0", CONSOLE_GROUP, "demo", "demo", "main")
        catalog = CommandCatalog([provider])
        assert catalog.find_providers("demo") == (provider,)

    def test_rows_round_trip(self):
        catalog = build_catalog(
            [
                (
                    "demo",
                    "1.0",
                    '[console_scripts]\ndemo = demo:main ; python_version >= "3.10"\n',
                )
            ]
        )
        assert CommandCatalog.from_rows(catalog.rows()).rows() == catalog.rows()

    def test_build_catalog_from_mappings(self):
        catalog = build_catalog(
            [
                {
                    "distribution": "demo",
                    "version": "1.0",
                    "entry_points": "[console_scripts]\ndemo = demo:main\n",
                }
            ]
        )
        assert len(catalog.find_providers("demo")) == 1

    def test_add_release_file(self, tmp_path):
        path = tmp_path / "entry_points.txt"
        path.write_text("[console_scripts]\ndemo = demo:main\n", encoding="utf-8")

        catalog = CommandCatalog()
        catalog.add_release_file("demo", "1.0", path)
        assert catalog.find_providers("demo")[0].version == "1.0"


class TestQueryResult:
    def test_repr_and_iteration(self):
        provider = CommandProvider("demo", "1.0", CONSOLE_GROUP, "demo", "demo", "main")
        result = QueryResult("demo", ((CONSOLE_GROUP, (provider,)),))
        assert list(result) == [provider]
        assert result.for_group("console") == (provider,)
        assert result.unique("console") == provider
