import re

import cobble

from .. import results, lists
from . import office_xml
from .xmlparser import XmlElement, XmlText
from .relationships_xml import read_relationships_xml_element, Relationships


_CUSTOM_XML_RELATIONSHIP_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/customXml"
)
_OFFICE_DOCUMENT_RELATIONSHIP_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
)

_SDT = "w:sdt"
_SDT_PROPERTIES = "w:sdtPr"
_SDT_CONTENT = "w:sdtContent"
_DATA_BINDING = "w:dataBinding"
_SHOWING_PLACEHOLDER = "w:showingPlcHdr"
_DATASTORE_ITEM = "ds:datastoreItem"

_KNOWN_PREFIXES = dict(office_xml.namespace_mappings())


@cobble.data
class ContentControl(object):
    control_id = cobble.field()
    name = cobble.field()
    tag = cobble.field()
    group_index = cobble.field()
    store_item_id = cobble.field()
    xpath = cobble.field()
    effective_xpath = cobble.field()
    repeat_path = cobble.field()
    prefix_mappings = cobble.field()
    display_text = cobble.field()
    displayed_value = cobble.field()
    bound_value = cobble.field()
    is_placeholder = cobble.field()
    placeholder_text = cobble.field()


@cobble.data
class ContentControlGroup(object):
    index = cobble.field()
    store_item_id = cobble.field()
    repeat_path = cobble.field()
    controls = cobble.field()


def extract_control_groups(fileobj):
    return _extract(fileobj)


def extract_content_controls(fileobj):
    return _extract(fileobj).map(lambda groups: lists.flat_map(
        lambda group: group.controls,
        groups,
    ))


def _extract(fileobj):
    from ..zips import open_zip

    messages = []
    with open_zip(fileobj, "r") as zip_file:
        stores = _read_data_stores(zip_file, messages)
        document_name = _find_document_part(zip_file)
        document_root = _read_xml_part(zip_file, document_name)

    if document_root is None:
        return results.Result([], messages)

    sdt_infos = _collect_sdt_infos(document_root)
    bound_infos = [info for info in sdt_infos if info["binding"] is not None]

    container_elements = set(
        id(info["element"])
        for info in bound_infos
        if _has_bound_descendant(info["element"])
    )

    assignments = _assign_nodes(bound_infos, stores, messages)

    groups = []
    group_key_by_element = {}
    controls_by_key = {}

    for info in bound_infos:
        if id(info["element"]) in container_elements:
            continue

        container = _nearest_bound_container(info["ancestors"], container_elements)
        if container is None:
            group_key = ("standalone", id(info["element"]))
            store_item_id = info["binding"]["store_item_id"]
            repeat_path = None
        else:
            group_key = ("container", id(container))
            container_info = _find_info_for_element(bound_infos, container)
            store_item_id = container_info["binding"]["store_item_id"]
            context_node = assignments[id(container)]
            repeat_path = _canonical_path_for_node(
                container_info, context_node, stores,
            )

        if group_key not in group_key_by_element:
            group_index = len(groups)
            group_key_by_element[group_key] = group_index
            group = ContentControlGroup(
                index=group_index,
                store_item_id=store_item_id,
                repeat_path=repeat_path,
                controls=[],
            )
            groups.append(group)
            controls_by_key[group_key] = group.controls

        controls_by_key[group_key].append(_build_control(
            info=info,
            group_index=group_key_by_element[group_key],
            repeat_path=repeat_path,
            assigned_node=assignments[id(info["element"])],
            stores=stores,
        ))

    return results.Result(groups, messages)


def _assign_nodes(bound_infos, stores, messages):
    assignments = {}
    occurrence_by_key = {}

    for info in bound_infos:
        ancestor = _nearest_bound_ancestor(info["ancestors"], bound_infos)
        context_node = (
            None
            if ancestor is None
            else assignments[id(ancestor)]
        )
        nodes, _ = _evaluate_binding(info, stores, context_node, messages)
        binding = info["binding"]
        assignment_key = (
            binding["store_item_id"],
            binding["xpath"],
            id(context_node),
        )

        assigned_node = nodes[0] if nodes else None
        if len(nodes) > 1:
            occurrence = occurrence_by_key.get(assignment_key, 0)
            occurrence_by_key[assignment_key] = occurrence + 1
            if occurrence < len(nodes):
                assigned_node = nodes[occurrence]

        assignments[id(info["element"])] = assigned_node

    return assignments


def _build_control(info, group_index, repeat_path, assigned_node, stores):
    binding = info["binding"]
    store = stores.get(binding["store_item_id"])
    mappings = _parse_prefix_mappings(binding["prefix_mappings"])

    bound_value = None
    effective_xpath = binding["xpath"]
    if assigned_node is not None:
        if isinstance(assigned_node, tuple):
            bound_value = assigned_node[1]
        else:
            bound_value = _inner_text(assigned_node)
        effective_xpath = _effective_path(
            assigned_node, store, mappings, binding["xpath"],
        )

    display_text = info["display_text"]
    is_placeholder = info["is_placeholder"]

    return ContentControl(
        control_id=info["control_id"],
        name=info["name"],
        tag=info["tag"],
        group_index=group_index,
        store_item_id=binding["store_item_id"],
        xpath=binding["xpath"],
        effective_xpath=effective_xpath,
        repeat_path=repeat_path,
        prefix_mappings=binding["prefix_mappings"],
        display_text=display_text,
        displayed_value=None if is_placeholder else display_text,
        bound_value=bound_value,
        is_placeholder=is_placeholder,
        placeholder_text=display_text if is_placeholder else None,
    )


def _canonical_path_for_node(container_info, context_node, stores):
    if context_node is None:
        return None
    binding = container_info["binding"]
    store = stores.get(binding["store_item_id"])
    if store is None:
        return None
    mappings = _parse_prefix_mappings(binding["prefix_mappings"])
    return store.canonical_path(context_node, mappings)


def _evaluate_binding(info, stores, context_node, messages):
    binding = info["binding"]
    store = stores.get(binding["store_item_id"])
    mappings = _parse_prefix_mappings(binding["prefix_mappings"])
    if store is None:
        messages.append(results.warning(
            "Content control references unknown custom XML data store: {0}".format(
                binding["store_item_id"],
            )
        ))
        return [], binding["xpath"]

    nodes, effective_xpath = evaluate_xpath(
        store, binding["xpath"], mappings, context_node,
    )
    if not nodes:
        messages.append(results.warning(
            "Content control binding did not match any XML nodes: {0}".format(
                binding["xpath"],
            )
        ))
    return nodes, effective_xpath


def _read_data_stores(zip_file, messages):
    stores = {}
    for name in zip_file.names():
        if not name.endswith(".xml"):
            continue
        props_element = _read_xml_part(zip_file, name)
        if props_element is None or props_element.name != _DATASTORE_ITEM:
            continue

        item_id = props_element.attributes.get("ds:itemID")
        if item_id is None:
            continue

        data_part = _resolve_data_part(zip_file, name)
        if data_part is None:
            messages.append(results.warning(
                "Could not find custom XML part for data store item {0}".format(item_id)
            ))
            continue

        data_root = _read_xml_part(zip_file, data_part)
        if data_root is not None:
            stores[item_id] = _DataStore(item_id, data_root)

    return stores


def _resolve_data_part(zip_file, props_part):
    relationships = _read_relationships(
        zip_file, _relationships_part_name(props_part),
    )
    for target in relationships.find_targets_by_type(_CUSTOM_XML_RELATIONSHIP_TYPE):
        resolved = _resolve_relative_part(props_part, target)
        if zip_file.exists(resolved):
            return resolved

    directory, basename = _split_part_name(props_part)
    match = re.match(r"^itemProps(\d+)\.xml$", basename)
    if match is not None:
        candidate = _join_part_name(directory, "item{0}.xml".format(match.group(1)))
        if zip_file.exists(candidate):
            return candidate

    return None


def _find_document_part(zip_file):
    relationships = _read_relationships(zip_file, "_rels/.rels")
    for target in relationships.find_targets_by_type(_OFFICE_DOCUMENT_RELATIONSHIP_TYPE):
        candidate = target.lstrip("/")
        if zip_file.exists(candidate):
            return candidate
    return "word/document.xml"


def _read_xml_part(zip_file, name):
    if not zip_file.exists(name):
        return None
    try:
        with zip_file.open(name) as fileobj:
            return office_xml.read(fileobj)
    except Exception:
        return None


def _read_relationships(zip_file, name):
    if not zip_file.exists(name):
        return Relationships.EMPTY
    with zip_file.open(name) as fileobj:
        element = office_xml.read(fileobj)
    return read_relationships_xml_element(element)


def _collect_sdt_infos(document_root):
    infos = []

    def walk(element, ancestors):
        for child in element.children:
            if not isinstance(child, XmlElement):
                continue
            if child.name == _SDT:
                infos.append(_sdt_info(child, ancestors))
                walk(child, ancestors + [child])
            else:
                walk(child, ancestors)

    walk(document_root, [])
    return infos


def _sdt_info(element, ancestors):
    properties = element.find_child_or_null(_SDT_PROPERTIES)
    data_binding = properties.find_child(_DATA_BINDING)
    binding = None
    if data_binding is not None and data_binding.attributes.get("w:xpath"):
        binding = {
            "xpath": data_binding.attributes.get("w:xpath"),
            "store_item_id": data_binding.attributes.get("w:storeItemID"),
            "prefix_mappings": data_binding.attributes.get("w:prefixMappings"),
        }

    content_element = element.find_child(_SDT_CONTENT)
    display_text = _inner_text(content_element) if content_element is not None else ""

    return {
        "element": element,
        "ancestors": ancestors,
        "binding": binding,
        "display_text": display_text,
        "is_placeholder": properties.find_child(_SHOWING_PLACEHOLDER) is not None,
        "control_id": properties.find_child_or_null("w:id").attributes.get("w:val"),
        "name": _read_sdt_property(properties, ("w:alias", "w:name")),
        "tag": _read_sdt_property(properties, ("w:tag",)),
    }


def _read_sdt_property(properties, element_names):
    for element_name in element_names:
        element = properties.find_child(element_name)
        if element is not None:
            return element.attributes.get("w:val")
    return None


def _has_bound_descendant(element):
    for descendant in _descendants(element):
        if descendant.name != _SDT:
            continue
        properties = descendant.find_child_or_null(_SDT_PROPERTIES)
        data_binding = properties.find_child(_DATA_BINDING)
        if data_binding is not None and data_binding.attributes.get("w:xpath"):
            return True
    return False


def _descendants(element):
    for child in element.children:
        if isinstance(child, XmlElement):
            yield child
            for descendant in _descendants(child):
                yield descendant


def _nearest_bound_container(ancestors, container_elements):
    for ancestor in reversed(ancestors):
        if id(ancestor) in container_elements:
            return ancestor
    return None


def _nearest_bound_ancestor(ancestors, bound_infos):
    bound_elements = set(id(info["element"]) for info in bound_infos)
    for ancestor in reversed(ancestors):
        if id(ancestor) in bound_elements:
            return ancestor
    return None


def _find_info_for_element(infos, element):
    for info in infos:
        if info["element"] is element:
            return info
    return None


def _effective_path(node, store, mappings, original_xpath):
    if store is None:
        return original_xpath
    if isinstance(node, tuple):
        element, _ = node
        attribute_step = original_xpath.rsplit("/", 1)[-1]
        return store.canonical_path(element, mappings) + "/" + attribute_step
    return store.canonical_path(node, mappings)


class _DataStore(object):
    def __init__(self, item_id, root):
        self.item_id = item_id
        self.root = root
        self._parents = self._build_parents(root)

    def _build_parents(self, root):
        parents = {}

        def walk(element, parent):
            parents[id(element)] = parent
            for child in element.children:
                if isinstance(child, XmlElement):
                    walk(child, element)

        walk(root, None)
        return parents

    def canonical_path(self, node, mappings):
        uri_to_prefix = dict((uri, prefix) for prefix, uri in mappings.items())
        parts = []
        current = node
        while isinstance(current, XmlElement):
            uri, local_name = split_element_name(current.name)
            prefix = uri_to_prefix.get(uri)
            if prefix is None:
                rendered_name = local_name
            else:
                rendered_name = "{0}:{1}".format(prefix, local_name)

            parent = self._parents.get(id(current))
            siblings = [
                child
                for child in (parent.children if parent is not None else [current])
                if isinstance(child, XmlElement) and child.name == current.name
            ]
            position = _identity_index(siblings, current) + 1
            parts.append("{0}[{1}]".format(rendered_name, position))
            current = parent

        return "/" + "/".join(reversed(parts))


def _identity_index(elements, target):
    for index, element in enumerate(elements):
        if element is target:
            return index
    return -1


def evaluate_xpath(store, xpath, mappings, context_node):
    if store is None or not xpath:
        return [], xpath

    steps = parse_xpath(xpath)
    if not steps:
        return [], xpath

    if steps[0].is_absolute:
        current_nodes = steps[0].select(_virtual_document_node(store.root), mappings)
        steps = steps[1:]
    elif context_node is not None:
        current_nodes = [context_node]
    else:
        current_nodes = [store.root]

    for step in steps:
        next_nodes = []
        for node in current_nodes:
            if not isinstance(node, XmlElement):
                continue
            next_nodes.extend(step.select(node, mappings))
        current_nodes = next_nodes

    return current_nodes, xpath


def _virtual_document_node(root):
    return XmlElement("#document", {}, [root])


@cobble.data
class _XPathStep(object):
    name = cobble.field()
    predicates = cobble.field()
    is_absolute = cobble.field()

    def select(self, element, mappings):
        if self.name.startswith("@"):
            attribute_name = self.name[1:]
            value = _find_attribute(element, attribute_name, mappings)
            return [(element, value)] if value is not None else []

        uri, local_name = _resolve_step_name(self.name, mappings)
        candidates = [
            child
            for child in element.children
            if isinstance(child, XmlElement)
            and _element_matches(child, uri, local_name)
        ]
        return _apply_predicates(candidates, self.predicates, mappings)


def _apply_predicates(candidates, predicates, mappings):
    filtered = list(candidates)
    for predicate in predicates:
        predicate = predicate.strip()
        if predicate == "last()":
            filtered = [filtered[-1]] if filtered else []
        elif re.match(r"^\d+$", predicate):
            index = int(predicate) - 1
            filtered = [filtered[index]] if 0 <= index < len(filtered) else []
        elif predicate.startswith("@"):
            filtered = _filter_by_attribute(predicate, filtered, mappings)
        else:
            filtered = _filter_by_child_text(predicate, filtered, mappings)
    return filtered


def _filter_by_attribute(predicate, candidates, mappings):
    match = re.match(r"^@([\w:.-]+)\s*=\s*(['\"])(.*?)\2$", predicate)
    if match is None:
        return candidates
    attribute_name, _, expected = match.groups()
    return [
        candidate
        for candidate in candidates
        if _find_attribute(candidate, attribute_name, mappings) == expected
    ]


def _filter_by_child_text(predicate, candidates, mappings):
    match = re.match(r"^([\w:.-]+)\s*=\s*(['\"])(.*?)\2$", predicate)
    if match is None:
        return candidates
    child_name, _, expected = match.groups()
    uri, local_name = _resolve_step_name(child_name, mappings)
    result = []
    for candidate in candidates:
        for child in candidate.children:
            if isinstance(child, XmlElement) and _element_matches(child, uri, local_name):
                if _inner_text(child) == expected:
                    result.append(candidate)
                    break
    return result


def _find_attribute(element, attribute_name, mappings):
    uri, local_name = _resolve_step_name(attribute_name, mappings)
    for name, value in element.attributes.items():
        attribute_uri, attribute_local = split_element_name(name)
        if attribute_local == local_name and (uri is None or attribute_uri == uri):
            return value
    return None


def _resolve_step_name(name, mappings):
    if ":" in name:
        prefix, local_name = name.split(":", 1)
        return mappings.get(prefix) or _KNOWN_PREFIXES.get(prefix), local_name
    return None, name


def _element_matches(element, uri, local_name):
    element_uri, element_local = split_element_name(element.name)
    if element_local != local_name:
        return False
    return uri is None or element_uri == uri


def parse_xpath(xpath):
    is_absolute = xpath.startswith("/")
    steps = []
    for token in _split_path(xpath):
        if not token:
            continue
        name, predicates = _split_step(token)
        steps.append(_XPathStep(name, predicates, is_absolute=False))
    if is_absolute and steps:
        steps[0] = cobble.copy(steps[0], is_absolute=True)
    return steps


def _split_path(xpath):
    tokens = []
    current = []
    quote = None
    for character in xpath:
        if quote is not None:
            current.append(character)
            if character == quote:
                quote = None
        elif character in ("'", '"'):
            quote = character
            current.append(character)
        elif character == "/":
            tokens.append("".join(current))
            current = []
        else:
            current.append(character)
    tokens.append("".join(current))
    return tokens


def _split_step(token):
    predicates = []
    depth = 0
    name_parts = []
    current = []
    for character in token:
        if character == "[":
            if depth == 0:
                name_parts.append("".join(current))
                current = []
            else:
                current.append(character)
            depth += 1
        elif character == "]":
            depth -= 1
            if depth == 0:
                predicates.append("".join(current))
                current = []
            else:
                current.append(character)
        else:
            current.append(character)
    name_parts.append("".join(current))
    return "".join(name_parts), predicates


def _parse_prefix_mappings(prefix_mappings):
    if not prefix_mappings:
        return {}
    return dict(re.findall(
        r"xmlns:([\w.-]+)\s*=\s*['\"](.*?)['\"]",
        prefix_mappings,
    ))


def split_element_name(name):
    if name.startswith("{"):
        uri, local_name = name[1:].split("}", 1)
        return uri, local_name
    if ":" in name:
        _, local_name = name.split(":", 1)
        return None, local_name
    return None, name


def _inner_text(element):
    if element is None:
        return ""
    parts = []

    def walk(node):
        if isinstance(node, XmlText):
            parts.append(node.value)
        elif isinstance(node, XmlElement):
            for child in node.children:
                walk(child)

    walk(element)
    return "".join(parts)


def _relationships_part_name(part_name):
    directory, basename = _split_part_name(part_name)
    return _join_part_name(_join_part_name(directory, "_rels"), basename + ".rels")


def _resolve_relative_part(base_part, target):
    if target.startswith("/"):
        return target.lstrip("/")
    directory, _ = _split_part_name(base_part)
    parts = [
        part
        for part in (directory + "/" + target).split("/")
        if part and part != "."
    ]
    resolved = []
    for part in parts:
        if part == "..":
            if resolved:
                resolved.pop()
        else:
            resolved.append(part)
    return "/".join(resolved)


def _split_part_name(name):
    if "/" in name:
        return name.rsplit("/", 1)
    return "", name


def _join_part_name(directory, name):
    return name if not directory else directory + "/" + name
