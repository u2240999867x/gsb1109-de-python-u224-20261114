import collections

from .. import zips
from . import office_xml
from .relationships_xml import EXTERNAL_TARGET_MODE, read_relationships_xml_element
from .uris import resolve_relative_uri


_RELS_EXTENSION = ".rels"
_RELS_DIRECTORY = "_rels"


class PartRelationship(collections.namedtuple("PartRelationship", [
    "part",
    "relationship_id",
    "relationship_type",
    "target",
    "target_mode",
    "is_external",
    "is_dangling",
])):
    pass


class PartRelationships(object):
    def __init__(self, relationships):
        self._relationships = sorted(
            relationships,
            key=lambda relationship: (relationship.part, relationship.relationship_id),
        )

    def __iter__(self):
        return iter(self._relationships)

    def __len__(self):
        return len(self._relationships)

    def __eq__(self, other):
        return isinstance(other, PartRelationships) and \
            self._relationships == other._relationships

    def __ne__(self, other):
        return not self.__eq__(other)

    def find(self, part, relationship_id):
        for relationship in self._relationships:
            if relationship.part == part and \
                    relationship.relationship_id == relationship_id:
                return relationship
        raise KeyError((part, relationship_id))


def read_part_relationships(fileobj):
    zip_file = zips.open_zip(fileobj, "r")
    relationships = []

    for rels_path in _find_relationships_parts(zip_file):
        part_name, base_path = _owning_part(rels_path)
        element = _read_element(zip_file, rels_path)
        part_relationships = read_relationships_xml_element(element)

        for relationship in part_relationships.relationships:
            target, is_dangling = _resolve_target(relationship, base_path, zip_file.exists)
            relationships.append(PartRelationship(
                part=part_name,
                relationship_id=relationship.relationship_id,
                relationship_type=relationship.type,
                target=target,
                target_mode=relationship.target_mode,
                is_external=relationship.target_mode == EXTERNAL_TARGET_MODE,
                is_dangling=is_dangling,
            ))

    return PartRelationships(relationships)


def _resolve_target(relationship, base_path, exists):
    if relationship.target_mode == EXTERNAL_TARGET_MODE:
        return relationship.target, False

    target = resolve_relative_uri(base_path, relationship.target)
    if exists(target):
        return target, False
    return target, True


def _find_relationships_parts(zip_file):
    return sorted(
        name
        for name in zip_file.names()
        if _is_relationships_part(name)
    )


def _is_relationships_part(name):
    dirname, basename = zips.split_path(name)
    return zips.split_path(dirname)[1] == _RELS_DIRECTORY and \
        basename.endswith(_RELS_EXTENSION)


def _owning_part(rels_path):
    dirname, basename = zips.split_path(rels_path)
    base_path = zips.split_path(dirname)[0]
    part_basename = basename[:-len(_RELS_EXTENSION)]
    return zips.join_path(base_path, part_basename), base_path


def _read_element(zip_file, name):
    with zip_file.open(name) as fileobj:
        return office_xml.read(fileobj)
