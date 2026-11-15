import textwrap
import zipfile

import pytest

from installer.inventory import (
    ARTIFACT_ONLY_FIELDS,
    MULTI_VALUE_FIELDS,
    Artifact,
    Field,
    FieldValue,
    InvalidArtifactMetadata,
    Inventory,
    Metadata,
    VersionView,
    WheelInventory,
    build_inventory,
    canonicalize_specifier_set,
    canonicalize_version,
    group_artifacts,
    group_wheels,
    inventory_from_wheels,
)
from installer.utils import parse_metadata_file


def make_wheel(
    tmp_path,
    filename,
    *,
    name="Demo",
    version="1.0",
    requires_python=">=3.9",
    extra_metadata="",
    wheel_tag=None,
    requires_dist=(),
):
    """Create a small but structurally valid wheel."""
    path = tmp_path / filename
    dist_info = f"{name}-{version}.dist-info"
    tag = wheel_tag or "py3-none-any"

    lines = [
        "Metadata-Version: 2.4",
        f"Name: {name}",
        f"Version: {version}",
    ]
    if requires_python is not None:
        lines.append(f"Requires-Python: {requires_python}")
    for requirement in requires_dist:
        lines.append(f"Requires-Dist: {requirement}")
    if extra_metadata:
        lines.extend(textwrap.dedent(extra_metadata).strip("\n").splitlines())

    metadata = "\n".join(lines) + "\n"
    wheel = f"""\
Wheel-Version: 1.0
Generator: test
Root-Is-Purelib: true
Tag: {tag}
"""
    record = f"{dist_info}/METADATA,,\n{dist_info}/WHEEL,,\n{dist_info}/RECORD,,\n"

    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{name}/__init__.py", "VALUE = 1\n")
        archive.writestr(f"{dist_info}/METADATA", metadata)
        archive.writestr(f"{dist_info}/WHEEL", wheel)
        archive.writestr(f"{dist_info}/RECORD", record)
    return path


def test_artifact_reads_record_files(tmp_path):
    wheel = make_wheel(tmp_path, "demo-1.0-py3-none-any.whl")
    artifact = Artifact.from_path(wheel)

    assert "Demo-1.0.dist-info/METADATA" in artifact.files
    assert "Demo-1.0.dist-info/WHEEL" in artifact.files


def test_artifact_dependency_metadata(tmp_path):
    wheel = make_wheel(
        tmp_path,
        "demo-1.0-py3-none-any.whl",
        requires_dist=("click>=8", "rich"),
        extra_metadata="Requires-External: libffi\nProvides-Extra: docs\n",
    )
    artifact = Artifact.from_path(wheel)

    assert artifact.requires_external == ("libffi",)
    assert artifact.provides_extra == ("docs",)
    assert artifact.dependency_metadata["Requires-Dist"] == ("click>=8", "rich")
    assert artifact.as_dict()["files"]


def test_version_canonicalization():
    assert canonicalize_version("1.0.0") == "1"
    assert canonicalize_version("v1.0") == "1"
    assert canonicalize_version("1.0RC1") == "1rc1"
    assert canonicalize_version("1!1.0.0") == "1!1"
    assert canonicalize_version("1.0+ABC.1") == "1+abc.1"


def test_specifier_canonicalization():
    assert canonicalize_specifier_set(">=3.8,<4") == "<4,>=3.8"
    assert canonicalize_specifier_set("< 4 , >= 3.8.0") == "<4,>=3.8"
    # Compatible releases and wildcards keep their segment count.
    assert canonicalize_specifier_set("~=1.4") != canonicalize_specifier_set("~=1.4.0")
    assert canonicalize_specifier_set("==1.0.*") != canonicalize_specifier_set(
        "==1.0.0.*"
    )


class TestInventoryGrouping:
    def test_groups_normalized_name_and_version(self, tmp_path):
        first = make_wheel(tmp_path, "Demo-1.0-py3-none-any.whl")
        second = make_wheel(tmp_path, "demo-1.0.0-py3-none-any.whl")

        inventory = build_inventory([first, second])

        assert len(inventory) == 1
        (view,) = inventory.versions
        assert view.canonical_name == "demo"
        assert view.canonical_version == "1"
        assert len(view) == 2

    def test_different_filenames_do_not_create_versions(self, tmp_path):
        wheels = [
            make_wheel(
                tmp_path,
                "demo-1.0-cp310-cp310-linux_x86_64.whl",
                wheel_tag="cp310-cp310-linux_x86_64",
            ),
            make_wheel(
                tmp_path,
                "demo-1.0-cp310-cp310-macosx_11_0_arm64.whl",
                wheel_tag="cp310-cp310-macosx_11_0_arm64",
            ),
            make_wheel(
                tmp_path,
                "demo-1.0-1-cp310-cp310-win_amd64.whl",
                wheel_tag="cp310-cp310-win_amd64",
            ),
        ]

        inventory = Inventory.from_paths(wheels)

        assert len(inventory) == 1
        view = inventory.get("demo", "1.0")
        assert view is not None
        assert len(view.artifacts) == 3

    def test_different_versions_stay_separate(self, tmp_path):
        first = make_wheel(tmp_path, "demo-1.0-py3-none-any.whl")
        second = make_wheel(
            tmp_path,
            "demo-2.0-py3-none-any.whl",
            version="2.0",
        )

        inventory = Inventory.from_paths([first, second])

        assert len(inventory) == 2
        assert [view.version for view in inventory.versions] == ["1.0", "2.0"]

    def test_different_projects_stay_separate(self, tmp_path):
        first = make_wheel(tmp_path, "demo-1.0-py3-none-any.whl")
        second = make_wheel(tmp_path, "other-1.0-py3-none-any.whl", name="other")

        assert len(Inventory.from_paths([first, second])) == 2

    def test_lookup_normalizes_arguments(self, tmp_path):
        wheel = make_wheel(tmp_path, "Demo-1.0.0-py3-none-any.whl")
        inventory = Inventory.from_paths([wheel])

        assert ("DEMO", "1.0") in inventory
        assert "demo" in inventory
        view = inventory["demo", "1"]
        assert view.version == "1.0"

    def test_ambiguous_version_lookup_raises(self, tmp_path):
        first = make_wheel(tmp_path, "demo-1.0-py3-none-any.whl")
        second = make_wheel(tmp_path, "demo-2.0-py3-none-any.whl", version="2.0")
        inventory = Inventory.from_paths([first, second])

        with pytest.raises(LookupError):
            inventory.get("demo")


class TestVersionHoisting:
    def test_hoists_only_consistent_project_fields(self, tmp_path):
        first = make_wheel(
            tmp_path,
            "demo-1.0-cp310-cp310-linux_x86_64.whl",
            wheel_tag="cp310-cp310-linux_x86_64",
            extra_metadata="""
                Summary: A demo project
                Classifier: Programming Language :: Python
                Requires-Dist: dep; sys_platform == "linux"
            """,
        )
        second = make_wheel(
            tmp_path,
            "demo-1.0-cp310-cp310-win_amd64.whl",
            wheel_tag="cp310-cp310-win_amd64",
            extra_metadata="""
                Summary: A demo project
                Classifier: Programming Language :: Python
                Requires-Dist: dep; sys_platform == "win32"
            """,
        )

        inventory = Inventory.from_paths([first, second])
        view = inventory.get("demo", "1.0")
        assert view is not None

        assert view.get("Summary") == "A demo project"
        assert view.get("Classifier") == "Programming Language :: Python"

        # Platform-specific and dependency data stay on the artifacts.
        assert view.platform_metadata[first.name]["Tag"] == (
            "cp310-cp310-linux_x86_64",
        )
        assert view.platform_metadata[second.name]["Tag"] == ("cp310-cp310-win_amd64",)
        assert view.dependencies[first.name] == ('dep; sys_platform == "linux"',)
        assert view.dependencies[second.name] == ('dep; sys_platform == "win32"',)
        assert view.get("Requires-Dist") is None

    def test_multi_value_fields_compare_as_sets(self, tmp_path):
        first = make_wheel(
            tmp_path,
            "demo-1.0-py3-none-any.whl",
            extra_metadata="""
                Classifier: A
                Classifier: B
            """,
        )
        second = make_wheel(
            tmp_path,
            "demo-1.0-1-py3-none-any.whl",
            extra_metadata="""
                Classifier: B
                Classifier: A
                Classifier: A
            """,
        )

        inventory = Inventory.from_paths([first, second])
        view = inventory.get("demo", "1.0")
        assert view is not None

        classifier = view.field("Classifier")
        assert classifier is not None
        assert classifier.consistent
        assert view.get("Classifier") == ("A", "B")

    def test_multi_value_difference_is_reported(self, tmp_path):
        first = make_wheel(
            tmp_path,
            "demo-1.0-py3-none-any.whl",
            extra_metadata="Classifier: A\nClassifier: B\n",
        )
        second = make_wheel(
            tmp_path,
            "demo-1.0-1-py3-none-any.whl",
            extra_metadata="Classifier: A\n",
        )

        view = Inventory.from_paths([first, second]).get("demo", "1.0")
        assert view is not None
        classifier = view.field("Classifier")
        assert classifier is not None
        assert classifier.divergent
        assert view.get("Classifier") is None

    def test_requires_python_divergence_has_artifact_provenance(self, tmp_path):
        first = make_wheel(
            tmp_path,
            "demo-1.0-cp310-cp310-linux_x86_64.whl",
            requires_python=">=3.9",
            wheel_tag="cp310-cp310-linux_x86_64",
        )
        second = make_wheel(
            tmp_path,
            "demo-1.0-cp310-cp310-macosx_11_0_arm64.whl",
            requires_python=">=3.9",
            wheel_tag="cp310-cp310-macosx_11_0_arm64",
        )
        third = make_wheel(
            tmp_path,
            "demo-1.0-cp311-cp311-win_amd64.whl",
            requires_python=">=3.11",
            wheel_tag="cp311-cp311-win_amd64",
        )

        inventory = Inventory.from_paths([first, second, third])
        view = inventory.get("demo", "1.0")
        assert view is not None

        assert view.requires_python is None
        field = view.field("Requires-Python")
        assert field is not None
        assert field.divergent
        groups = field.groups
        assert len(groups) == 2
        assert field.artifacts_for(field.values[2].value) == (view.artifacts[2],)
        assert groups[1].filenames == (third.name,)


class TestArtifactLayer:
    def test_artifact_keeps_wheel_and_dependency_data(self, tmp_path):
        wheel = make_wheel(
            tmp_path,
            "demo-1.0-7-cp310-cp310-manylinux_2_17_x86_64.whl",
            wheel_tag="cp310-cp310-manylinux_2_17_x86_64",
            requires_dist=("click>=8", "rich"),
        )
        artifact = Artifact.from_path(wheel)

        assert artifact.build_tag == "7"
        assert artifact.tags == ("cp310-cp310-manylinux_2_17_x86_64",)
        assert artifact.is_platform_specific
        assert artifact.requires_dist == ("click>=8", "rich")
        assert artifact.wheel_metadata.get("Wheel-Version") == "1.0"

    def test_pure_python_artifact(self, tmp_path):
        wheel = make_wheel(tmp_path, "demo-1.0-py3-none-any.whl")
        artifact = Artifact.from_path(wheel)

        assert not artifact.is_platform_specific
        assert artifact.tags == ("py3-none-any",)

    def test_metadata_mapping_preserves_multi_values(self):
        metadata = Metadata(
            {"Classifier": ["A", "B"], "summary": "A demo", "Name": "demo"}
        )

        assert metadata.get("SUMMARY") == "A demo"
        assert metadata.get_all("classifier") == ("A", "B")
        assert metadata["name"] == ("demo",)
        assert "Summary" in metadata
        with pytest.raises(KeyError):
            metadata["missing"]
        assert metadata.as_dict() == {
            "Classifier": ("A", "B"),
            "summary": "A demo",
            "Name": "demo",
        }

    def test_metadata_equality_and_repr(self):
        first = Metadata({"Summary": "A demo"})
        second = Metadata({"summary": "A demo"})
        assert first == second
        assert first != object()
        assert repr(first) == "Metadata({'Summary': 'A demo'})"


class TestFieldModel:
    def test_field_describe_and_provenance(self, tmp_path):
        first = make_wheel(
            tmp_path, "demo-1.0-py3-none-any.whl", requires_python=">=3.9"
        )
        second = make_wheel(
            tmp_path,
            "demo-1.0-1-py3-none-any.whl",
            requires_python=">=3.11",
        )
        view = Inventory.from_paths([first, second]).get("demo", "1.0")
        assert view is not None
        field = view.field("Requires-Python")
        assert field is not None

        assert field.consistent is False
        assert "from" in field.describe()
        assert field.provenance[view.artifacts[0]] == ">=3.9"

    def test_consistent_field(self, tmp_path):
        first = make_wheel(
            tmp_path, "demo-1.0-py3-none-any.whl", requires_python=">=3.9"
        )
        second = make_wheel(
            tmp_path, "demo-1.0-1-py3-none-any.whl", requires_python=">=3.9.0"
        )
        view = Inventory.from_paths([first, second]).get("demo", "1.0")
        assert view is not None
        field = view.field("Requires-Python")
        assert field is not None

        assert field.consistent
        assert field.describe() == "Requires-Python: '>=3.9'"

    def test_field_value_model(self, tmp_path):
        wheel = make_wheel(tmp_path, "demo-1.0-py3-none-any.whl")
        artifact = Artifact.from_path(wheel)
        value = FieldValue(artifact=artifact, raw="x", value="x")
        field = Field(name="X", values=(value,))

        assert field.consistent
        assert field.value == "x"
        assert field.groups[0].artifacts == (artifact,)


class TestConstants:
    def test_artifact_only_fields(self):
        assert {"requires-dist", "requires-external"} <= ARTIFACT_ONLY_FIELDS
        assert "platform" not in ARTIFACT_ONLY_FIELDS
        assert "classifier" not in ARTIFACT_ONLY_FIELDS

    def test_multi_value_fields(self):
        assert {"classifier", "requires-dist", "project-url"} <= MULTI_VALUE_FIELDS
        assert "summary" not in MULTI_VALUE_FIELDS


def test_view_properties(tmp_path):
    first = make_wheel(
        tmp_path,
        "demo-1.0-py3-none-any.whl",
        extra_metadata="Summary: A demo\n",
    )
    second = make_wheel(
        tmp_path,
        "demo-1.0-1-py3-none-any.whl",
        extra_metadata="Summary: A demo\n",
    )
    view = Inventory.from_paths([first, second]).get("demo", "1.0")
    assert view is not None

    assert view.project_metadata["Summary"] == "A demo"
    assert set(view.consistent_fields) == {
        "Metadata-Version",
        "Name",
        "Version",
        "Requires-Python",
        "Summary",
    }
    assert view.has_divergences is False
    assert view.artifact_metadata[first.name].get("Summary") == "A demo"
    assert view.get_artifact(first.name) is not None
    assert view.get_artifact("missing") is None
    assert "Summary" in view
    assert list(view) == sorted(
        view.fields, key=lambda name: list(view.fields).index(name)
    )
    assert view.as_dict()["metadata"]["Summary"] == "A demo"
    assert repr(view).startswith("VersionView(")


def test_inventory_json_shape(tmp_path):
    wheel = make_wheel(tmp_path, "demo-1.0-py3-none-any.whl")
    inventory = Inventory.from_paths([wheel])

    data = inventory.as_dict()
    assert data["versions"][0]["canonical_name"] == "demo"
    assert data["versions"][0]["artifacts"][0]["filename"] == wheel.name
    assert repr(inventory) == "Inventory(versions=1, artifacts=1)"


def test_artifact_layer_projections_and_divergence_sources(tmp_path):
    first = make_wheel(
        tmp_path,
        "demo-1.0-1-py3-none-any.whl",
        requires_python=">=3.9",
        requires_dist=("click>=8",),
    )
    second = make_wheel(
        tmp_path,
        "demo-1.0-2-py3-none-any.whl",
        requires_python=">=3.11",
        requires_dist=("rich",),
        extra_metadata="Requires-External: libffi\n",
    )
    view = Inventory.from_paths([first, second]).get("demo", "1.0")
    assert view is not None

    assert view.requires_python is None
    assert view.requires_python_by_artifact == {
        first.name: ">=3.9",
        second.name: ">=3.11",
    }
    assert view.build_tags == {first.name: "1", second.name: "2"}
    assert view.dependencies == {
        first.name: ("click>=8",),
        second.name: ("rich",),
    }
    assert view.dependency_metadata[second.name]["Requires-External"] == ("libffi",)
    assert view.files[first.name]
    assert view.tags[first.name] == ("py3-none-any",)

    assert view.divergence_sources("Requires-Python") == {
        ">=3.9": (first.name,),
        ">=3.11": (second.name,),
    }
    assert view.divergence_sources("Summary") == {}
    assert view.divergence_sources("Missing") == {}
    assert view.artifacts_for("Missing", "x") == ()


def test_requires_python_shared_after_canonicalization(tmp_path):
    first = make_wheel(tmp_path, "demo-1.0-py3-none-any.whl", requires_python=">=3.9")
    second = make_wheel(
        tmp_path, "demo-1.0-1-py3-none-any.whl", requires_python=">= 3.9.0"
    )
    view = Inventory.from_paths([first, second]).get("demo", "1.0")
    assert view is not None

    assert view.requires_python == ">=3.9"
    assert view.divergence_sources("Requires-Python") == {}


def test_inventory_contains_and_iteration(tmp_path):
    wheel = make_wheel(tmp_path, "demo-1.0-py3-none-any.whl")
    inventory = Inventory.from_paths([wheel])

    assert ("demo", "1") in inventory
    assert ("demo", "2") not in inventory
    assert ("not-a-tuple",) not in inventory
    assert 42 not in inventory
    assert list(inventory) == list(inventory.versions)
    with pytest.raises(KeyError):
        inventory["missing"]
    with pytest.raises(KeyError):
        inventory["demo", "2"]


class TestEdgeCases:
    def test_version_parts_branches(self):
        assert canonicalize_version("1.0.post1") == "1.post1"
        assert canonicalize_version("1.0.dev1") == "1.dev1"
        assert canonicalize_version("1.0-1") == "1.post1"
        assert canonicalize_version("1.0post") == "1.post0"
        assert canonicalize_version("1.0dev") == "1.dev0"
        assert canonicalize_version("not-a-version") == "not-a-version"
        assert canonicalize_version("  NOT-A-VERSION  ") == "not-a-version"

    def test_specifier_branches(self):
        assert canonicalize_specifier_set("") == ""
        assert canonicalize_specifier_set(">=3.8,") == ">=3.8"
        assert canonicalize_specifier_set("garbage") == "garbage"
        assert canonicalize_specifier_set("===1.0") == "===1.0"
        assert canonicalize_specifier_set("~=1.0rc1") == "~=1.0rc1"
        assert canonicalize_specifier_set("~=1.0.post1") == "~=1.0.post1"
        assert canonicalize_specifier_set("~=1.0.dev1") == "~=1.0.dev1"
        assert canonicalize_specifier_set("~=1.0+abc") == "~=1.0+abc"
        assert canonicalize_specifier_set("==1.0.*") == "==1.0.*"
        assert canonicalize_specifier_set("garbage") != ">=3.8"

    def test_normalize_text_branches(self, tmp_path):
        first = make_wheel(
            tmp_path,
            "demo-1.0-py3-none-any.whl",
            extra_metadata="""
                Author-email: a@example.com, b@example.com
                Project-URL: Home, https://example.com
                Project-URL: Docs, https://docs.example.com
                Description-Content-Type: text/markdown
                License-File: LICENSE
                License-File: AUTHORS
                Description: line one
                    line two
            """,
        )
        second = make_wheel(
            tmp_path,
            "demo-1.0-1-py3-none-any.whl",
            extra_metadata="""
                Author-email: a@example.com,b@example.com
                Project-URL: Home, https://example.com
                Project-URL: Docs, https://docs.example.com
                Description-Content-Type: Text/Markdown
                License-File: LICENSE
                License-File: AUTHORS
                Description: line one line two
            """,
        )
        view = Inventory.from_paths([first, second]).get("demo", "1.0")
        assert view is not None

        assert view.get("Author-email") == "a@example.com, b@example.com"
        assert view.get("Project-URL") == (
            "Home, https://example.com",
            "Docs, https://docs.example.com",
        )
        assert view.has_divergences

    def test_metadata_iteration_helpers(self):
        metadata = Metadata({"Classifier": ["A", "B"], "Summary": "S"})

        assert list(metadata.items()) == [
            ("Classifier", ("A", "B")),
            ("Summary", ("S",)),
        ]
        assert list(metadata.keys()) == ["Classifier", "Summary"]
        assert list(metadata.values()) == [("A", "B"), ("S",)]
        assert len(metadata) == 2

    def test_metadata_from_message_payload(self):
        message = parse_metadata_file("Name: demo\nVersion: 1.0\n\nBody text\n")
        metadata = Metadata.from_message(message, include_payload=True)
        assert metadata.get("Description") == "Body text"

    def test_artifact_errors(self, tmp_path):
        bad = tmp_path / "demo-1.0-py3-none-any.whl"
        bad.write_bytes(b"not a zip")
        with pytest.raises(InvalidArtifactMetadata):
            Artifact.from_path(bad)

        empty = tmp_path / "empty-1.0-py3-none-any.whl"
        with zipfile.ZipFile(empty, "w"):
            pass
        with pytest.raises(InvalidArtifactMetadata):
            Artifact.from_path(empty)

    def test_artifact_without_tag_uses_filename(self, tmp_path):
        wheel = make_wheel(tmp_path, "demo-1.0-py3-none-any.whl")
        artifact = Artifact.from_path(wheel)
        artifact = Artifact(
            filename=artifact.filename,
            distribution=artifact.distribution,
            version=artifact.version,
            metadata=artifact.metadata,
            wheel_metadata=Metadata({}),
            tag=artifact.tag,
        )
        assert artifact.tags == ("py3-none-any",)

    def test_version_scalar_with_tuple_raw(self):
        metadata = Metadata({"Summary": ["A", "B"]})
        field = Field(
            name="Summary",
            values=(
                FieldValue(
                    artifact=Artifact("x.whl", "demo", "1.0", metadata, Metadata({})),
                    raw=("A", "B"),
                    value=("A", "B"),
                ),
            ),
        )
        assert field.consistent
        assert field.normalized_value == ("A", "B")
        assert field.value == ("A", "B")

    def test_field_groups_and_describe(self, tmp_path):
        first = make_wheel(
            tmp_path, "demo-1.0-py3-none-any.whl", requires_python=">=3.9"
        )
        second = make_wheel(
            tmp_path, "demo-1.0-1-py3-none-any.whl", requires_python=">=3.11"
        )
        view = Inventory.from_paths([first, second]).get("demo", "1.0")
        assert view is not None
        field = view.field("Requires-Python")
        assert field is not None

        assert field.normalized_value is None
        assert field.describe().count("from") == 2
        assert view.consistent_fields["Name"].describe()

    def test_inventory_helpers(self, tmp_path):
        first = make_wheel(tmp_path, "demo-1.0-py3-none-any.whl")
        second = make_wheel(
            tmp_path, "demo-1.0-1-py3-none-any.whl", requires_python=">=3.11"
        )
        inventory = Inventory.from_artifacts(
            [Artifact.from_path(first), Artifact.from_path(second)]
        )

        assert inventory.version_views == inventory.versions
        assert inventory.get("demo") == inventory.versions[0]
        assert inventory["demo"] == inventory.versions[0]
        assert inventory.to_dict() == inventory.as_dict()
        assert list(inventory) == list(inventory.versions)

    def test_inventory_missing_name(self, tmp_path):
        wheel = make_wheel(tmp_path, "demo-1.0-py3-none-any.whl")
        inventory = Inventory.from_paths([wheel])
        assert inventory.get("missing") is None
        with pytest.raises(KeyError):
            inventory["missing"]

    def test_version_view_empty(self):
        with pytest.raises(ValueError):
            VersionView([])

    def test_version_view_equality(self, tmp_path):
        wheel = make_wheel(tmp_path, "demo-1.0-py3-none-any.whl")
        inventory = Inventory.from_paths([wheel])
        view = inventory.versions[0]
        assert view == VersionView(inventory.artifacts)
        assert view != object()
        assert hash(view) == hash(VersionView(inventory.artifacts))

    def test_group_helpers(self, tmp_path):
        wheel = make_wheel(tmp_path, "demo-1.0-py3-none-any.whl")
        artifact = Artifact.from_path(wheel)
        assert len(group_artifacts([artifact])) == 1
        assert len(group_wheels([wheel])) == 1
        assert len(inventory_from_wheels([wheel])) == 1

    def test_inventory_alias_methods(self, tmp_path):
        wheel = make_wheel(tmp_path, "demo-1.0-py3-none-any.whl")
        inventory = Inventory()
        artifact = inventory.add_wheel(wheel)
        assert artifact.filename == wheel.name
        inventory2 = WheelInventory.from_wheels([wheel])
        assert inventory2 == inventory or inventory2.versions == inventory.versions


class TestCoverageBranches:
    def test_wildcard_canonicalization(self):
        assert canonicalize_specifier_set("==1.0.*") == "==1.0.*"
        assert canonicalize_specifier_set("==v1.0.*") == "==1.0.*"
        assert canonicalize_specifier_set("==01.00.*") == "==1.0.*"

    def test_description_content_type_alone(self, tmp_path):
        first = make_wheel(
            tmp_path,
            "demo-1.0-py3-none-any.whl",
            extra_metadata="Description-Content-Type: text/markdown\n",
        )
        second = make_wheel(
            tmp_path,
            "demo-1.0-1-py3-none-any.whl",
            extra_metadata="Description-Content-Type: Text/Markdown\n",
        )
        view = Inventory.from_paths([first, second]).get("demo", "1.0")
        assert view is not None
        assert view.get("Description-Content-Type") == "text/markdown"

    def test_project_url_missing_separator(self, tmp_path):
        first = make_wheel(
            tmp_path, "demo-1.0-py3-none-any.whl", extra_metadata="Project-URL: Home\n"
        )
        second = make_wheel(
            tmp_path,
            "demo-1.0-1-py3-none-any.whl",
            extra_metadata="Project-URL: Home\n",
        )
        view = Inventory.from_paths([first, second]).get("demo", "1.0")
        assert view is not None
        assert view.get("Project-URL") == "Home"

    def test_single_artifact_project_field_with_duplicate_values(self, tmp_path):
        wheel = make_wheel(
            tmp_path,
            "demo-1.0-py3-none-any.whl",
            extra_metadata="Summary: A\nSummary: B\n",
        )
        view = Inventory.from_paths([wheel]).get("demo", "1.0")
        assert view is not None
        field = view.field("Summary")
        assert field is not None
        assert field.consistent
        assert field.value == ("A", "B")

    def test_artifact_to_dict(self, tmp_path):
        wheel = make_wheel(tmp_path, "demo-1.0-py3-none-any.whl")
        artifact = Artifact.from_path(wheel)
        assert artifact.to_dict() == artifact.as_dict()

    def test_field_empty_values_is_consistent(self):
        field = Field(name="X", values=())
        assert field.consistent
        assert field.value is None
        assert field.groups == ()

    def test_all_artifacts_missing_field_is_skipped(self, tmp_path):
        # Requires-Python is omitted by make_wheel via None.
        wheel = make_wheel(
            tmp_path,
            "demo-1.0-py3-none-any.whl",
            requires_python=None,
        )
        view = Inventory.from_paths([wheel]).get("demo", "1.0")
        assert view is not None
        assert view.field("Requires-Python") is None
        assert view.requires_python is None

    def test_version_view_layer_aliases(self, tmp_path):
        wheel = make_wheel(tmp_path, "demo-1.0-py3-none-any.whl")
        view = Inventory.from_paths([wheel]).get("demo", "1.0")
        assert view is not None
        assert view.version_layer == view.project_metadata
        assert view.artifact_layer == view.artifacts

    def test_version_view_to_dict(self, tmp_path):
        wheel = make_wheel(tmp_path, "demo-1.0-py3-none-any.whl")
        view = Inventory.from_paths([wheel]).get("demo", "1.0")
        assert view is not None
        assert view.to_dict() == view.as_dict()

    def test_version_view_getitem_missing(self, tmp_path):
        wheel = make_wheel(tmp_path, "demo-1.0-py3-none-any.whl")
        view = Inventory.from_paths([wheel]).get("demo", "1.0")
        assert view is not None
        with pytest.raises(KeyError):
            view["Missing"]
        assert view["version"] == "1.0"

    def test_inventory_from_sources(self, tmp_path):
        from installer.sources import WheelFile

        wheel = make_wheel(tmp_path, "demo-1.0-py3-none-any.whl")
        with WheelFile.open(wheel) as source:
            inventory = Inventory.from_sources([source])
        assert inventory.get("demo", "1.0") is not None
        assert inventory.get("demo", "1.0").artifacts[0].filename == ""


class TestRemainingBranches:
    def test_record_unavailable(self):
        from installer.inventory import _read_record_paths
        from installer.sources import WheelSource

        assert _read_record_paths(WheelSource("demo", "1.0")) == ()

    def test_record_bad_path_bubbles(self, tmp_path):
        wheel = make_wheel(tmp_path, "demo-1.0-py3-none-any.whl")
        import zipfile

        files = {}
        with zipfile.ZipFile(wheel) as archive:
            for name in archive.namelist():
                files[name] = archive.read(name)
        files["Demo-1.0.dist-info/RECORD"] = b"bad,row\n"
        with zipfile.ZipFile(wheel, "w") as archive:
            for name, data in files.items():
                archive.writestr(name, data)
        from installer.records import InvalidRecordEntry

        with pytest.raises(InvalidRecordEntry):
            Artifact.from_path(wheel)

    def test_source_missing_metadata(self):
        from installer.sources import WheelSource

        class EmptySource(WheelSource):
            def read_dist_info(self, filename):
                raise KeyError(filename)

        with pytest.raises(InvalidArtifactMetadata):
            Artifact.from_source(EmptySource("demo", "1.0"))

    def test_metadata_repeated_header_after_first(self):
        metadata = Metadata({"Classifier": ["A"], "CLASSIFIER": ["B"]})
        assert metadata.get_all("classifier") == ("A", "B")

    def test_metadata_payload_already_has_description(self):
        message = parse_metadata_file("Description: header\n\nBody\n")
        metadata = Metadata.from_message(message, include_payload=True)
        assert metadata.get("Description") == "header"

    def test_multi_value_scalar_raw(self):
        # A single value for a multi-use field is still normalized as a set.
        metadata = Metadata({"Classifier": "A"})
        field = Field(
            name="Classifier",
            values=(
                FieldValue(
                    artifact=Artifact("x.whl", "demo", "1.0", metadata, Metadata({})),
                    raw="A",
                    value=frozenset({"A"}),
                ),
            ),
        )
        assert field.consistent

    def test_field_single_raw_none(self):
        metadata = Metadata({})
        field = Field(
            name="Summary",
            values=(
                FieldValue(
                    artifact=Artifact("x.whl", "demo", "1.0", metadata, Metadata({})),
                    raw=None,
                    value=None,
                ),
            ),
        )
        assert field.consistent
        assert field.value is None
        assert field.normalized_value is None

    def test_all_artifacts_missing_different_fields(self, tmp_path):
        first = make_wheel(tmp_path, "demo-1.0-py3-none-any.whl", requires_python=None)
        second = make_wheel(
            tmp_path, "demo-1.0-1-py3-none-any.whl", requires_python=None
        )
        view = Inventory.from_paths([first, second]).get("demo", "1.0")
        assert view is not None
        assert "Requires-Python" not in view.fields


def test_field_present_on_only_one_artifact(tmp_path):
    first = make_wheel(
        tmp_path,
        "demo-1.0-py3-none-any.whl",
        extra_metadata="Summary: A demo\n",
    )
    second = make_wheel(tmp_path, "demo-1.0-1-py3-none-any.whl")
    view = Inventory.from_paths([first, second]).get("demo", "1.0")
    assert view is not None

    summary = view.field("Summary")
    assert summary is not None
    assert summary.divergent
    assert summary.values[1].raw is None
    assert summary.values[1].value is None
    assert view.get("Summary") is None


def test_invalid_specifier_passthrough():
    assert canonicalize_specifier_set("~=not-a-version") == "~=not-a-version"


def test_invalid_wildcard_falls_back_to_version_canonicalization():
    # ``==abc.*`` does not have a numeric release prefix, so it falls back to
    # plain version canonicalization.
    assert canonicalize_specifier_set("==abc.*") == "==abc.*"
