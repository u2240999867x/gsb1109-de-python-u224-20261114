# encoding=utf-8

import io
import zipfile

from mammoth import extract_content_controls, extract_control_groups
from mammoth.docx.content_controls import parse_xpath
from ..testing import assert_equal


_WORDML_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_CUSTOM_NS = "http://example.com/purchase"
_STORE_ID = "{7f3f9c2a-0b7e-4d9f-8c1a-6b2a4d0e1f11}"
_PREFIX_MAPPINGS = "xmlns:po='{0}'".format(_CUSTOM_NS)


def test_controls_are_grouped_by_outer_repeating_item():
    result = extract_control_groups(_purchase_docx(
        groups=[
            {"supplier": "甲供应商", "tax": "是", "amount": "1001.00"},
            {"supplier": "乙供应商", "tax": "否", "amount": "1002.00"},
            {"supplier": "丙供应商", "tax": "是", "amount": "1003.00"},
        ],
        placeholders=set(),
    ))

    assert_equal([], result.messages)
    assert_equal(3, len(result.value))

    for index, group in enumerate(result.value):
        assert_equal(index, group.index)
        assert_equal(_STORE_ID, group.store_item_id)
        assert_equal(
            "/po:采购单[1]/po:明细[{0}]".format(index + 1),
            group.repeat_path,
        )
        assert_equal(3, len(group.controls))
        assert_equal(
            ["供应商", "含税", "金额"],
            [control.name for control in group.controls],
        )
        for control in group.controls:
            assert_equal(index, control.group_index)


def test_displayed_value_and_bound_value_are_both_retained():
    result = extract_control_groups(_purchase_docx(
        groups=[
            {
                "supplier": ("甲供应商", "甲供应商（显示）"),
                "tax": ("是", "是"),
                "amount": ("1001.00", "¥1,001.00"),
            },
        ],
        placeholders=set(),
    ))

    control = result.value[0].controls[2]
    assert_equal("¥1,001.00", control.display_text)
    assert_equal("¥1,001.00", control.displayed_value)
    assert_equal("1001.00", control.bound_value)
    assert_equal(False, control.is_placeholder)


def test_placeholder_text_is_not_treated_as_filled_value_but_bound_value_is_kept():
    result = extract_control_groups(_purchase_docx(
        groups=[
            {"supplier": "甲供应商", "tax": "是", "amount": "1001.00"},
            {"supplier": "乙供应商", "tax": "否", "amount": "1002.00"},
        ],
        placeholders={(1, "amount")},
        placeholder_text="单击此处输入金额",
    ))

    amount_control = result.value[1].controls[2]
    assert_equal(True, amount_control.is_placeholder)
    assert_equal(None, amount_control.displayed_value)
    assert_equal("单击此处输入金额", amount_control.placeholder_text)
    assert_equal("单击此处输入金额", amount_control.display_text)
    assert_equal("1002.00", amount_control.bound_value)

    filled_control = result.value[0].controls[2]
    assert_equal(False, filled_control.is_placeholder)
    assert_equal("1001.00", filled_control.displayed_value)
    assert_equal(None, filled_control.placeholder_text)


def test_controls_sharing_a_label_are_distinguished_by_binding_path_and_group():
    result = extract_content_controls(_purchase_docx(
        groups=[
            {"supplier": "甲供应商", "tax": "是", "amount": "1001.00"},
            {"supplier": "乙供应商", "tax": "否", "amount": "1002.00"},
        ],
        placeholders=set(),
        duplicate_supplier_labels=True,
    ))

    suppliers = [
        control
        for control in result.value
        if control.name == "供应商"
    ]
    assert_equal(2, len(suppliers))
    assert_equal([0, 1], [control.group_index for control in suppliers])
    assert_equal(
        [
            "/po:采购单[1]/po:明细[1]/po:供应商[1]",
            "/po:采购单[1]/po:明细[2]/po:供应商[1]",
        ],
        [control.effective_xpath for control in suppliers],
    )
    assert_equal(
        ["甲供应商", "乙供应商"],
        [control.bound_value for control in suppliers],
    )


def test_reordered_visible_controls_still_pair_amount_with_original_supplier():
    result = extract_control_groups(_purchase_docx(
        groups=[
            {"supplier": "甲供应商", "tax": "是", "amount": "1001.00"},
            {"supplier": "乙供应商", "tax": "否", "amount": "1002.00"},
            {"supplier": "丙供应商", "tax": "是", "amount": "1003.00"},
        ],
        placeholders=set(),
        reorder_groups={1},
    ))

    expected = [
        ("甲供应商", "是", "1001.00"),
        ("乙供应商", "否", "1002.00"),
        ("丙供应商", "是", "1003.00"),
    ]
    for group, (supplier, tax, amount) in zip(result.value, expected):
        by_name = dict((control.name, control) for control in group.controls)
        assert_equal(supplier, by_name["供应商"].bound_value)
        assert_equal(tax, by_name["含税"].bound_value)
        assert_equal(amount, by_name["金额"].bound_value)

    assert_equal(
        ["金额", "含税", "供应商"],
        [control.name for control in result.value[1].controls],
    )


def test_relative_bindings_on_repeated_sections_resolve_to_distinct_instances():
    result = extract_control_groups(_relative_repeating_docx())

    assert_equal(3, len(result.value))
    expected = [
        ("供应商1", "是", "1001.00"),
        ("供应商2", "否", "1002.00"),
        ("供应商3", "是", "1003.00"),
    ]
    for index, (group, values) in enumerate(zip(result.value, expected)):
        assert_equal(
            "/po:采购单[1]/po:明细[{0}]".format(index + 1),
            group.repeat_path,
        )
        bound_values = [control.bound_value for control in group.controls]
        assert_equal(list(values), bound_values)


def test_attribute_bindings_read_attribute_values():
    result = extract_control_groups(_attribute_docx())

    amount_control = result.value[0].controls[1]
    assert_equal("CNY1", amount_control.bound_value)
    assert_equal(
        "/po:采购单[1]/po:明细[1]/po:金额[1]/@币种",
        amount_control.effective_xpath,
    )


def test_unknown_data_store_emits_warning_and_keeps_displayed_value():
    result = extract_content_controls(_purchase_docx(
        groups=[
            {"supplier": "甲供应商", "tax": "是", "amount": "1001.00"},
        ],
        placeholders=set(),
        unknown_store=True,
    ))

    assert_equal(1, len(result.messages))
    control = result.value[0]
    assert_equal(None, control.bound_value)
    assert_equal("甲供应商", control.displayed_value)


def test_parse_xpath_splits_absolute_path_with_predicates():
    steps = parse_xpath("/po:root/po:items[2]/po:item[@code='A1']/@value")

    assert_equal(4, len(steps))
    assert_equal(True, steps[0].is_absolute)
    assert_equal("po:root", steps[0].name)
    assert_equal(["2"], steps[1].predicates)
    assert_equal(["@code='A1'"], steps[2].predicates)
    assert_equal("@value", steps[3].name)
    assert_equal(False, steps[3].is_absolute)


def _purchase_docx(
        groups,
        placeholders,
        placeholder_text="占位提示",
        duplicate_supplier_labels=False,
        reorder_groups=None,
        unknown_store=False):
    reorder_groups = reorder_groups or set()
    store_id = "unknown-store" if unknown_store else _STORE_ID

    def control_xml(alias, xpath, text, is_placeholder):
        placeholder_element = "<w:showingPlcHdr/>" if is_placeholder else ""
        return (
            "<w:sdt><w:sdtPr>{placeholder}<w:alias w:val=\"{alias}\"/>"
            "<w:dataBinding w:xpath=\"{xpath}\" w:storeItemID=\"{store}\" "
            "w:prefixMappings=\"{mappings}\"/><w:id w:val=\"1\"/></w:sdtPr>"
            "<w:sdtContent><w:r><w:t>{text}</w:t></w:r></w:sdtContent></w:sdt>"
        ).format(
            placeholder=placeholder_element,
            alias=alias,
            xpath=xpath,
            store=store_id,
            mappings=_PREFIX_MAPPINGS,
            text=text,
        )

    group_parts = []
    for index, group in enumerate(groups):
        def field_xml(field_name):
            value = group[field_name]
            if isinstance(value, tuple):
                bound_value, display_value = value
            else:
                bound_value = display_value = value
            is_placeholder = (index, field_name) in placeholders
            text = placeholder_text if is_placeholder else display_value
            alias = "供应商" if duplicate_supplier_labels and field_name == "supplier" else _FIELD_LABELS[field_name]
            xpath = "/po:采购单/po:明细[{0}]/po:{1}".format(index + 1, _FIELD_ELEMENTS[field_name])
            return control_xml(alias, xpath, text, is_placeholder)

        children = [field_xml("supplier"), field_xml("tax"), field_xml("amount")]
        if index in reorder_groups:
            children = [children[2], children[1], children[0]]

        group_parts.append(control_xml(
            "明细组",
            "/po:采购单/po:明细[{0}]".format(index + 1),
            "",
            False,
        ).replace(
            "<w:sdtContent><w:r><w:t></w:t></w:r></w:sdtContent>",
            "<w:sdtContent>{0}</w:sdtContent>".format("".join(children)),
        ))

    return _build_docx(
        document_xml=_document_xml("".join(group_parts)),
        custom_xml=_custom_xml(groups),
        include_store=not unknown_store,
    )


def _relative_repeating_docx():
    def control_xml(alias, xpath, text):
        return (
            "<w:sdt><w:sdtPr><w:alias w:val=\"{alias}\"/>"
            "<w:dataBinding w:xpath=\"{xpath}\" w:storeItemID=\"{store}\" "
            "w:prefixMappings=\"{mappings}\"/><w:id w:val=\"1\"/></w:sdtPr>"
            "<w:sdtContent><w:r><w:t>{text}</w:t></w:r></w:sdtContent></w:sdt>"
        ).format(
            alias=alias,
            xpath=xpath,
            store=_STORE_ID,
            mappings=_PREFIX_MAPPINGS,
            text=text,
        )

    groups = [
        {"supplier": "供应商1", "tax": "是", "amount": "1001.00"},
        {"supplier": "供应商2", "tax": "否", "amount": "1002.00"},
        {"supplier": "供应商3", "tax": "是", "amount": "1003.00"},
    ]
    group_parts = []
    for index, group in enumerate(groups):
        children = "".join([
            control_xml("供应商", "po:供应商", group["supplier"]),
            control_xml("含税", "po:含税", group["tax"]),
            control_xml("金额", "po:金额", group["amount"]),
        ])
        group_parts.append(
            "<w:sdt><w:sdtPr><w:alias w:val=\"明细重复节\"/>"
            "<w:dataBinding w:xpath=\"/po:采购单/po:明细\" w:storeItemID=\"{store}\" "
            "w:prefixMappings=\"{mappings}\"/><w:id w:val=\"1\"/></w:sdtPr>"
            "<w:sdtContent>{children}</w:sdtContent></w:sdt>".format(
                store=_STORE_ID,
                mappings=_PREFIX_MAPPINGS,
                children=children,
            )
        )

    return _build_docx(
        document_xml=_document_xml("".join(group_parts)),
        custom_xml=_custom_xml(groups),
    )


def _attribute_docx():
    groups = [{"supplier": "甲供应商", "tax": "是", "amount": "1001.00", "currency": "CNY1"}]
    children = (
        "<w:sdt><w:sdtPr><w:alias w:val=\"供应商\"/>"
        "<w:dataBinding w:xpath=\"/po:采购单/po:明细[1]/po:供应商\" w:storeItemID=\"{store}\" "
        "w:prefixMappings=\"{mappings}\"/><w:id w:val=\"1\"/></w:sdtPr>"
        "<w:sdtContent><w:r><w:t>甲供应商</w:t></w:r></w:sdtContent></w:sdt>"
        "<w:sdt><w:sdtPr><w:alias w:val=\"金额\"/>"
        "<w:dataBinding w:xpath=\"/po:采购单/po:明细[1]/po:金额/@币种\" w:storeItemID=\"{store}\" "
        "w:prefixMappings=\"{mappings}\"/><w:id w:val=\"1\"/></w:sdtPr>"
        "<w:sdtContent><w:r><w:t>CNY1</w:t></w:r></w:sdtContent></w:sdt>"
    ).format(store=_STORE_ID, mappings=_PREFIX_MAPPINGS)
    container = (
        "<w:sdt><w:sdtPr><w:alias w:val=\"明细组\"/>"
        "<w:dataBinding w:xpath=\"/po:采购单/po:明细[1]\" w:storeItemID=\"{store}\" "
        "w:prefixMappings=\"{mappings}\"/><w:id w:val=\"1\"/></w:sdtPr>"
        "<w:sdtContent>{children}</w:sdtContent></w:sdt>"
    ).format(store=_STORE_ID, mappings=_PREFIX_MAPPINGS, children=children)

    custom_xml = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
        "<po:采购单 xmlns:po=\"{0}\"><po:明细>"
        "<po:供应商>甲供应商</po:供应商><po:含税>是</po:含税>"
        "<po:金额 币种=\"CNY1\">1001.00</po:金额>"
        "</po:明细></po:采购单>"
    ).format(_CUSTOM_NS)

    return _build_docx(
        document_xml=_document_xml(container),
        custom_xml=custom_xml,
    )


_FIELD_LABELS = {
    "supplier": "供应商",
    "tax": "含税",
    "amount": "金额",
}

_FIELD_ELEMENTS = {
    "supplier": "供应商",
    "tax": "含税",
    "amount": "金额",
}


def _custom_xml(groups):
    items = "".join(
        "<po:明细><po:供应商>{supplier}</po:供应商><po:含税>{tax}</po:含税>"
        "<po:金额>{amount}</po:金额></po:明细>".format(
            supplier=group["supplier"][0] if isinstance(group["supplier"], tuple) else group["supplier"],
            tax=group["tax"][0] if isinstance(group["tax"], tuple) else group["tax"],
            amount=group["amount"][0] if isinstance(group["amount"], tuple) else group["amount"],
        )
        for group in groups
    )
    return (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
        "<po:采购单 xmlns:po=\"{0}\">{1}</po:采购单>"
    ).format(_CUSTOM_NS, items)


def _document_xml(body):
    return (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<w:document xmlns:w=\"{0}\"><w:body><w:p>{1}</w:p></w:body></w:document>"
    ).format(_WORDML_NS, body)


def _build_docx(document_xml, custom_xml, include_store=True):
    root_relationships = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">"
        "<Relationship Id=\"rId1\" Type=\""
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument\" "
        "Target=\"word/document.xml\"/></Relationships>"
    )
    datastore_properties = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<ds:datastoreItem xmlns:ds=\""
        "http://schemas.openxmlformats.org/officeDocument/2006/customXml\" "
        "ds:itemID=\"{0}\"/>".format(_STORE_ID)
    )
    datastore_relationships = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">"
        "<Relationship Id=\"rId1\" Type=\""
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/customXml\" "
        "Target=\"item1.xml\"/></Relationships>"
    )

    fileobj = io.BytesIO()
    with zipfile.ZipFile(fileobj, "w") as zip_file:
        zip_file.writestr("_rels/.rels", root_relationships)
        zip_file.writestr("word/document.xml", document_xml)
        if include_store:
            zip_file.writestr("customXml/itemProps1.xml", datastore_properties)
            zip_file.writestr(
                "customXml/_rels/itemProps1.xml.rels",
                datastore_relationships,
            )
            zip_file.writestr("customXml/item1.xml", custom_xml)
    fileobj.seek(0)
    return fileobj
