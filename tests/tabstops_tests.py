from mammoth import documents
from mammoth.docx import body_xml
from mammoth.docx.numbering_xml import Numbering
from mammoth.docx.styles_xml import Styles, Style
from mammoth.docx.xmlparser import element as xml_element, text as xml_text
from mammoth.tabstops import (
    Column,
    columns,
    effective_tab_stops,
    field,
    layout_fields,
    paragraph_fields,
    reconstruct_columns,
)

from .testing import assert_equal


def test_style_tab_stops_are_used_when_paragraph_declares_none():
    stops = effective_tab_stops(
        style_stops=[documents.tab_stop(2160, "left")],
    )
    assert_equal([documents.tab_stop(2160, "left")], stops)


def test_clear_in_paragraph_cancels_style_tab_stop_at_same_position():
    stops = effective_tab_stops(
        style_stops=[documents.tab_stop(2160, "left"), documents.tab_stop(4320, "right")],
        cleared_positions=[2160],
    )
    assert_equal([documents.tab_stop(4320, "right")], stops)


def test_paragraph_tab_stop_overrides_style_tab_stop_at_same_position():
    stops = effective_tab_stops(
        style_stops=[documents.tab_stop(2160, "left")],
        paragraph_stops=[documents.tab_stop(2160, "decimal")],
    )
    assert_equal([documents.tab_stop(2160, "decimal")], stops)


def test_effective_tab_stops_are_sorted_by_position():
    stops = effective_tab_stops(
        style_stops=[documents.tab_stop(4320, "right")],
        paragraph_stops=[documents.tab_stop(2160, "left")],
    )
    assert_equal(
        [documents.tab_stop(2160, "left"), documents.tab_stop(4320, "right")],
        stops,
    )


def test_first_field_starts_at_start_of_line_without_tab_stop():
    placements = layout_fields(
        [documents.tab_stop(2000, "left")],
        [field(1500), field(300)],
    )
    assert_equal(None, placements[0].stop)
    assert_equal(0, placements[0].start)
    assert_equal(1500, placements[0].end)


def test_field_after_tab_aligns_to_next_left_stop():
    placements = layout_fields(
        [documents.tab_stop(2000, "left")],
        [field(1500), field(300)],
    )
    assert_equal(documents.tab_stop(2000, "left"), placements[1].stop)
    assert_equal(2000, placements[1].start)
    assert_equal(2300, placements[1].end)


def test_amount_aligns_to_right_stop_by_its_end():
    placements = layout_fields(
        [documents.tab_stop(5000, "right")],
        [field(1500), field(800)],
    )
    assert_equal(4200, placements[1].start)
    assert_equal(5000, placements[1].end)


def test_amount_aligns_to_decimal_stop_by_its_decimal_anchor():
    placements = layout_fields(
        [documents.tab_stop(6000, "decimal")],
        [field(1500), field(900, decimal_anchor=500)],
    )
    assert_equal(5500, placements[1].start)
    assert_equal(6400, placements[1].end)


def test_right_and_decimal_amounts_share_one_line():
    stops = [
        documents.tab_stop(2000, "left"),
        documents.tab_stop(5000, "right"),
        documents.tab_stop(7000, "decimal"),
    ]
    fields = [field(1500), field(300), field(800), field(900, decimal_anchor=500)]
    placements = layout_fields(stops, fields)
    assert_equal(2000, placements[1].start)
    assert_equal(4200, placements[2].start)
    assert_equal(6500, placements[3].start)


def test_tab_falls_back_to_default_interval_when_no_stop_can_be_reached():
    placements = layout_fields([], [field(100), field(100)])
    assert_equal(None, placements[1].stop)
    assert_equal(720, placements[1].start)


def test_lengthened_name_pushes_price_to_next_reachable_stop():
    stops = [documents.tab_stop(2000, "left"), documents.tab_stop(5000, "right")]

    original = layout_fields(stops, [field(1500), field(800)])
    assert_equal(2000, original[1].stop.position)

    # 改一个品名使其越过原本的停靠点：价格移动到实际能抵达的列
    edited = layout_fields(stops, [field(2500), field(800)])
    assert_equal(5000, edited[1].stop.position)
    assert_equal(4200, edited[1].start)
    assert_equal(5000, edited[1].end)


def test_field_with_unknown_width_and_following_fields_are_uncertain():
    stops = [documents.tab_stop(2000, "left"), documents.tab_stop(5000, "right")]
    placements = layout_fields(stops, [field(1500), field(None), field(800)])
    assert_equal(False, placements[0].uncertain)
    assert_equal(True, placements[1].uncertain)
    assert_equal(True, placements[2].uncertain)


def test_decimal_stop_without_decimal_anchor_is_uncertain():
    stops = [documents.tab_stop(6000, "decimal")]
    placements = layout_fields(stops, [field(1500), field(900)])
    assert_equal(documents.tab_stop(6000, "decimal"), placements[1].stop)
    assert_equal(True, placements[1].uncertain)


def test_columns_leave_stops_with_no_field_unfilled():
    stops = [
        documents.tab_stop(2000, "left"),
        documents.tab_stop(5000, "right"),
        documents.tab_stop(7000, "decimal"),
    ]
    placements = layout_fields(stops, [field(1500), field(800)])
    assert_equal(
        [
            Column(stop=documents.tab_stop(2000, "left"), field_index=1, uncertain=False),
            Column(stop=documents.tab_stop(5000, "right"), field_index=None, uncertain=False),
            Column(stop=documents.tab_stop(7000, "decimal"), field_index=None, uncertain=False),
        ],
        columns(stops, placements),
    )


def test_reconstruct_columns_marks_uncertain_columns():
    stops = [documents.tab_stop(2000, "left"), documents.tab_stop(5000, "right")]
    result = reconstruct_columns(stops, [field(1500), field(None)])
    assert_equal(
        [
            Column(stop=documents.tab_stop(2000, "left"), field_index=1, uncertain=True),
            Column(stop=documents.tab_stop(5000, "right"), field_index=None, uncertain=False),
        ],
        result,
    )


def test_paragraph_fields_splits_paragraph_text_on_tabs():
    paragraph = documents.paragraph(children=[
        documents.run([documents.text("Coffee")]),
        documents.tab(),
        documents.run([documents.text("2")]),
        documents.tab(),
        documents.run([documents.text("12."), documents.text("50")]),
    ])
    assert_equal(["Coffee", "2", "12.50"], paragraph_fields(paragraph))


def test_paragraph_fields_keeps_empty_segments_between_consecutive_tabs():
    paragraph = documents.paragraph(children=[
        documents.run([documents.text("Coffee")]),
        documents.tab(),
        documents.tab(),
        documents.run([documents.text("12.50")]),
    ])
    assert_equal(["Coffee", "", "12.50"], paragraph_fields(paragraph))


def test_start_offsets_first_field():
    placements = layout_fields(
        [documents.tab_stop(2000, "left")],
        [field(1500), field(300)],
        start=360,
    )
    assert_equal(360, placements[0].start)
    assert_equal(1860, placements[0].end)


def test_price_list_line_end_to_end():
    # 老价目表：一段一行，品名、数量和价格靠制表位对齐。
    # 样式声明的 2160 停靠点在段落里被取消，改在 2400；
    # 同一行里 5000 靠右对齐、7000 小数点对齐。
    styles = Styles.create(
        paragraph_styles={"PriceLine": Style(
            style_id="PriceLine",
            name="Price Line",
            tab_stops=(
                documents.tab_stop(2160, "left"),
                documents.tab_stop(5000, "right"),
                documents.tab_stop(7000, "decimal"),
            ),
        )},
    )
    paragraph_xml = xml_element("w:p", {}, [
        xml_element("w:pPr", {}, [
            xml_element("w:pStyle", {"w:val": "PriceLine"}),
            xml_element("w:tabs", {}, [
                xml_element("w:tab", {"w:val": "clear", "w:pos": "2160"}),
                xml_element("w:tab", {"w:val": "left", "w:pos": "2400"}),
            ]),
        ]),
        xml_element("w:r", {}, [xml_element("w:t", {}, [xml_text("Coffee")])]),
        xml_element("w:r", {}, [xml_element("w:tab")]),
        xml_element("w:r", {}, [xml_element("w:t", {}, [xml_text("2")])]),
        xml_element("w:r", {}, [xml_element("w:tab")]),
        xml_element("w:r", {}, [xml_element("w:t", {}, [xml_text("12.50")])]),
        xml_element("w:r", {}, [xml_element("w:tab")]),
        xml_element("w:r", {}, [xml_element("w:t", {}, [xml_text("104.00")])]),
    ])

    reader = body_xml.reader(numbering=Numbering.EMPTY, styles=styles)
    result = reader.read_all([paragraph_xml])
    assert_equal([], result.messages)
    paragraph = result.value[0]

    assert_equal(
        [
            documents.tab_stop(2400, "left"),
            documents.tab_stop(5000, "right"),
            documents.tab_stop(7000, "decimal"),
        ],
        paragraph.tab_stops,
    )
    assert_equal(["Coffee", "2", "12.50", "104.00"], paragraph_fields(paragraph))

    # 采集端实测的各段文字宽度（小数点对齐的列另给小数点前宽度）
    widths = [field(1200), field(200), field(900, decimal_anchor=500), field(1000, decimal_anchor=600)]

    assert_equal(
        [
            Column(stop=documents.tab_stop(2400, "left"), field_index=1, uncertain=False),
            Column(stop=documents.tab_stop(5000, "right"), field_index=2, uncertain=False),
            Column(stop=documents.tab_stop(7000, "decimal"), field_index=3, uncertain=False),
        ],
        reconstruct_columns(paragraph.tab_stops, widths),
    )

    # 改一个品名使其越过原本的停靠点：后面的价格移动到实际能抵达的列
    edited_widths = [field(2600)] + widths[1:]
    edited = layout_fields(paragraph.tab_stops, edited_widths)
    assert_equal(5000, edited[1].stop.position)
    assert_equal(7000, edited[2].stop.position)
    assert_equal(None, edited[3].stop)
    assert_equal(7920, edited[3].start)
