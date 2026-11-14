def read_settings_xml_element(element):
    even_and_odd = element.find_child("w:evenAndOddHeaders")
    even_and_odd_headers = _is_on(even_and_odd)
    return even_and_odd_headers


def _is_on(element):
    if element is None:
        return False
    value = element.attributes.get("w:val")
    return value not in ["false", "0"]
