from __future__ import unicode_literals

import io

from mammoth.docx import xmlparser as xml, office_xml
from ..testing import assert_equal


class AlternateContentTests(object):
    def test_when_fallback_is_present_then_fallback_is_read(self):
        xml_string = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
            '<numbering xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006">' +
            '<mc:AlternateContent>' +
            '<mc:Choice Requires="w14">' +
            '<choice/>' +
            '</mc:Choice>' +
            '<mc:Fallback>' +
            '<fallback/>' +
            '</mc:Fallback>' +
            '</mc:AlternateContent>' +
            '</numbering>')

        result = office_xml.read(io.StringIO(xml_string))
        assert_equal([xml.element("fallback")], result.children)


    def test_when_fallback_is_not_present_then_element_is_ignored(self):
        xml_string = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
            '<numbering xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006">' +
            '<mc:AlternateContent>' +
            '<mc:Choice Requires="w14">' +
            '<choice/>' +
            '</mc:Choice>' +
            '</mc:AlternateContent>' +
            '</numbering>')

        result = office_xml.read(io.StringIO(xml_string))
        assert_equal([], result.children)


    def test_when_choice_is_supported_then_choice_is_read(self):
        xml_string = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
            '<numbering xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"' +
            ' xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">' +
            '<mc:AlternateContent>' +
            '<mc:Choice Requires="w14">' +
            '<choice/>' +
            '</mc:Choice>' +
            '<mc:Fallback>' +
            '<fallback/>' +
            '</mc:Fallback>' +
            '</mc:AlternateContent>' +
            '</numbering>')

        result = office_xml.read(
            io.StringIO(xml_string),
            supported_namespaces=["http://schemas.microsoft.com/office/word/2010/wordml"],
        )
        assert_equal([xml.element("choice")], result.children)


    def test_when_two_choices_are_supported_then_first_choice_is_read(self):
        xml_string = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
            '<numbering xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"' +
            ' xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"' +
            ' xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml">' +
            '<mc:AlternateContent>' +
            '<mc:Choice Requires="w14"><first/></mc:Choice>' +
            '<mc:Choice Requires="w15"><second/></mc:Choice>' +
            '<mc:Fallback><fallback/></mc:Fallback>' +
            '</mc:AlternateContent>' +
            '</numbering>')

        result = office_xml.read(
            io.StringIO(xml_string),
            supported_namespaces=[
                "http://schemas.microsoft.com/office/word/2010/wordml",
                "http://schemas.microsoft.com/office/word/2012/wordml",
            ],
        )
        assert_equal([xml.element("first")], result.children)


    def test_when_choice_is_supported_then_fallback_is_not_read(self):
        xml_string = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
            '<numbering xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"' +
            ' xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">' +
            '<mc:AlternateContent>' +
            '<mc:Choice Requires="w14"><choice/></mc:Choice>' +
            '<mc:Fallback><fallback/></mc:Fallback>' +
            '</mc:AlternateContent>' +
            '</numbering>')

        result = office_xml.read(io.StringIO(xml_string), supported_namespaces=["w14"])
        assert_equal([xml.element("choice")], result.children)


    def test_when_choice_prefix_is_rebound_then_local_namespace_is_used(self):
        xml_string = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
            '<numbering xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"' +
            ' xmlns:w14="http://unrelated.example.com">' +
            '<mc:AlternateContent>' +
            '<mc:Choice xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" Requires="w14">' +
            '<choice/>' +
            '</mc:Choice>' +
            '<mc:Fallback><fallback/></mc:Fallback>' +
            '</mc:AlternateContent>' +
            '</numbering>')

        result = office_xml.read(
            io.StringIO(xml_string),
            supported_namespaces=["http://schemas.microsoft.com/office/word/2010/wordml"],
        )
        assert_equal([xml.element("choice")], result.children)
