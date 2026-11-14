import collections


Branch = collections.namedtuple(
    "Branch",
    ["children", "source", "kind", "requires", "index", "element"],
    defaults=(None, None, None, None, None),
)


class SupportedNamespaces(object):
    def __init__(self, declarations=None):
        self._uris = set()
        self._prefixes = set()
        self._prefix_uris = {}

        for declaration in _read_declarations(declarations):
            if isinstance(declaration, str):
                if _is_uri(declaration):
                    self._uris.add(declaration)
                else:
                    self._prefixes.add(declaration)
            else:
                prefix, uri = _read_pair(declaration)
                if uri is None:
                    self._prefixes.add(prefix)
                else:
                    self._prefix_uris[prefix] = uri
                    self._uris.add(uri)

    def supports(self, prefix, uri):
        if uri is not None:
            if uri in self._uris:
                return True
            elif prefix in self._prefix_uris:
                return self._prefix_uris[prefix] == uri
            else:
                return prefix in self._prefixes
        else:
            return prefix in self._prefixes or prefix in self._prefix_uris


def _read_declarations(declarations):
    if declarations is None:
        return []
    elif isinstance(declarations, str):
        return declarations.replace(",", " ").split()
    elif hasattr(declarations, "items"):
        return list(declarations.items())
    else:
        return list(declarations)


def _read_pair(declaration):
    first, second = declaration
    if _is_uri(second):
        return first, second
    elif _is_uri(first):
        return second, first
    else:
        return first, second


def _is_uri(value):
    return isinstance(value, str) and (
        "://" in value or value.startswith("urn:")
    )


def read(element, supported_namespaces=None):
    supported_namespaces = _as_supported_namespaces(supported_namespaces)

    for index, choice in enumerate(element.find_children("mc:Choice")):
        if _is_supported(choice, supported_namespaces):
            return Branch(
                children=choice.children,
                source="mc:Choice",
                kind="choice",
                requires=choice.attributes.get("Requires"),
                index=index,
                element=choice,
            )

    fallback = element.find_child("mc:Fallback")
    if fallback is None:
        return Branch(children=[])
    else:
        return Branch(
            children=fallback.children,
            source="mc:Fallback",
            kind="fallback",
            element=fallback,
        )


resolve = read


def describe(branch):
    if branch.source == "mc:Choice":
        return "mc:Choice (Requires={0})".format(branch.requires or "")
    elif branch.source == "mc:Fallback":
        return "mc:Fallback"
    else:
        return None


def _as_supported_namespaces(declarations):
    if isinstance(declarations, SupportedNamespaces):
        return declarations
    else:
        return SupportedNamespaces(declarations)


def _is_supported(choice, supported_namespaces):
    requires = choice.attributes.get("Requires")
    if not requires:
        return False

    return all(
        supported_namespaces.supports(prefix, choice.find_namespace_uri(prefix))
        for prefix in requires.split()
    )


