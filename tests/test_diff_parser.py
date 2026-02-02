from __future__ import annotations

from app.review.diff_parser import extract_changed_line_numbers


def test_extract_changed_line_numbers() -> None:
    diff = "\n".join(
        [
            "@@ -1,3 +1,4 @@",
            " line1",
            "-line2",
            "+line2_new",
            "+line3_new",
            " line4",
        ]
    )
    lines = extract_changed_line_numbers(diff=diff)
    assert lines == [2, 3]
