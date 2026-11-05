"""The source and answer of each line, as the log shows them."""

from __future__ import annotations

from bokucho.logs import render_translation_pairs

RED, GREEN, WHITE, RESET = "\x1b[31m", "\x1b[32m", "\x1b[37m", "\x1b[0m"
SEPARATOR = "─" * 40


def _rejected() -> dict:
    return {
        "source": "【アオイ】\n少し待ちます…っ。",
        "answer": "【阿奧伊】\n稍等一下…ね。",
        "marks": ["ね"],
        "reason": "not written in zh-Hant: ね",
    }


def test_a_rejected_line_ends_with_why_before_the_separator():
    assert render_translation_pairs([_rejected()]) == "\n".join(
        [
            "【アオイ】",
            "少し待ちます…っ。",
            ">",
            "【阿奧伊】",
            "稍等一下…ね。",
            "not written in zh-Hant: ね",
            SEPARATOR,
        ]
    )


def test_in_colour_a_rejected_line_is_red_where_it_is_wrong():
    lines = render_translation_pairs([_rejected()], color=True).split("\n")

    assert lines[2] == f"{RED}>{RESET}"
    assert lines[4] == f"稍等一下…{RED}ね{RESET}。"
    assert lines[5] == f"{RED}not written in zh-Hant: ね{RESET}"
    assert RED not in lines[1]


def test_an_accepted_line_keeps_its_green_marker_and_says_nothing_more():
    accepted = {"source": "げんき", "answer": "很好", "marks": [], "reason": ""}

    assert render_translation_pairs([accepted], color=True).split("\n") == [
        "げんき",
        f"{GREEN}>{RESET}",
        "很好",
        SEPARATOR,
    ]


def test_a_line_not_judged_yet_has_a_white_marker():
    shown = {"source": "げんき", "answer": "很好", "judged": False}

    assert render_translation_pairs([shown], color=True).split("\n")[1] == f"{WHITE}>{RESET}"
