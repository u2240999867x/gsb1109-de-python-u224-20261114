import xml.dom.minidom

import cobble


@cobble.data
class XmlElement(object):
    name = cobble.field()
    attributes = cobble.field()
    children = cobble.field()

    namespaces = None

    def find_child_or_null(self, name):
        return self.find_child(name) or null_xml_element

    def find_child(self, name):
        for child in self.children:
            if isinstance(child, XmlElement) and child.name == name:
                return child


    def find_children(self, name):
        return XmlElementList(filter(
            lambda child: child.node_type == node_types.element and child.name == name,
            self.children
        ))

    def find_namespace_uri(self, prefix):
        namespaces = self.namespaces or {}

        if prefix in namespaces:
            return namespaces[prefix]
        elif prefix:
            return self.attributes.get("xmlns:" + prefix)
        else:
            return self.attributes.get("xmlns")


class XmlElementList(object):
    def __init__(self, elements):
        self._elements = elements

    def __iter__(self):
        return iter(self._elements)

    def find_children(self, name):
        children = []
        for element in self._elements:
            if isinstance(element, XmlElement):
                for child in element.find_children(name):
                    children.append(child)
        return XmlElementList(children)


class NullXmlElement(object):
    attributes = {}
    children = []
    namespaces = {}

    def find_child_or_null(self, name):
        return self

    def find_child(self, name):
        return None

    def find_children(self, name):
        return XmlElementList([])

    def find_namespace_uri(self, prefix):
        return None


null_xml_element = NullXmlElement()


@cobble.data
class XmlText(object):
    value = cobble.field()


def element(name, attributes=None, children=None):
    return XmlElement(name, attributes or {}, children or [])

text = XmlText


class node_types(object):
    element = 1
    text = 3


XmlElement.node_type = node_types.element
XmlText.node_type = node_types.text



def parse_xml(fileobj, namespace_mapping=None):
    if namespace_mapping is None:
        namespace_prefixes = {}
    else:
        namespace_prefixes = dict((uri, prefix) for prefix, uri in namespace_mapping)

    document = xml.dom.minidom.parse(fileobj)

    def convert_node(node, inherited_namespaces):
        if node.nodeType == xml.dom.Node.ELEMENT_NODE:
            return convert_element(node, inherited_namespaces)
        elif node.nodeType == xml.dom.Node.TEXT_NODE:
            return XmlText(node.nodeValue)
        else:
            return None

    def convert_element(element, inherited_namespaces):
        converted_name = convert_name(element)
        namespaces = _read_namespaces(element, inherited_namespaces)

        converted_attributes = dict(
            (convert_name(attribute), attribute.value)
            for attribute in element.attributes.values()
            if attribute.namespaceURI != "http://www.w3.org/2000/xmlns/"
        )

        converted_children = []
        for child_node in element.childNodes:
            converted_child_node = convert_node(child_node, namespaces)
            if converted_child_node is not None:
                converted_children.append(converted_child_node)

        converted_element = XmlElement(converted_name, converted_attributes, converted_children)
        converted_element.namespaces = namespaces
        return converted_element

    def _read_namespaces(element, inherited_namespaces):
        namespaces = dict(inherited_namespaces)
        for attribute in element.attributes.values():
            if attribute.namespaceURI != "http://www.w3.org/2000/xmlns/":
                continue

            if attribute.name == "xmlns":
                namespaces[""] = attribute.value
            else:
                namespaces[attribute.localName] = attribute.value

        return namespaces

    def convert_name(node):
        if node.namespaceURI is None:
            return node.localName
        else:
            prefix = namespace_prefixes.get(node.namespaceURI)
            if prefix is None:
                return "{%s}%s" % (node.namespaceURI, node.localName)
            else:
                return "%s:%s" % (prefix, node.localName)

    return convert_node(document.documentElement, {})
