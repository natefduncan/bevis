"""The test for an edit near a claim (SPEC §6.2)."""

from bevis.review import claim_units, text_near_claim

C1 = frozenset({"c1"})


def block(text, bid="b1"):
    return {"id": bid, "type": "text", "text": text}


def claim(cid="c1", bid="b1"):
    return {"id": cid, "block_id": bid}


def near(before, after, before_claims=None, after_claims=None):
    claims = [claim()]
    return text_near_claim(block(before) if before else None, claims if before_claims is None else before_claims,
                           block(after) if after else None, claims if after_claims is None else after_claims)


def test_units_are_the_paragraphs_that_hold_a_claim_span():
    text = "Intro.\n\n[Revenue fell 12%]{#c1} last year.\n\nClosing."
    assert claim_units(text, C1) == ["Revenue fell 12% last year."]
    assert claim_units(text, frozenset()) == []
    assert claim_units(text, frozenset({"c9"})) == []


def test_list_items_and_headings_are_their_own_units():
    text = "- one\n- [two is 2]{#c1}\n- three\n\n## Heading with [4]{#c2}"
    assert claim_units(text, frozenset({"c1", "c2"})) == ["two is 2", "Heading with 4"]


def test_prose_away_from_a_claim_is_not_near():
    before = "Intro.\n\n[Revenue fell 12%]{#c1}.\n\nClosing."
    assert not near(before, before.replace("Intro.", "A new, longer introduction.").replace("Closing.", "Done."))
    assert not near(before, before + "\n\nOne more paragraph.")


def test_text_in_the_claims_paragraph_is_near():
    before = "[Revenue fell 12%]{#c1}. It was broad."
    assert near(before, "[Revenue fell 12%]{#c1}. It was broad and deep.")  # same paragraph, other sentence
    assert near(before, "Overall, [revenue fell 12%]{#c1}. It was broad.")
    assert near(before, "[Revenue fell 12%]{#c1}.")


def test_moving_a_span_to_another_paragraph_is_near():
    assert near("[Revenue fell 12%]{#c1}.\n\nClosing.", "Revenue was down.\n\n[Revenue fell 12%]{#c1}. Closing.")


def test_a_deleted_or_new_block_holding_a_span_is_near():
    assert near("[Revenue fell 12%]{#c1}.", None)
    assert text_near_claim(None, [], block("[Revenue fell 12%]{#c1}."), [claim()])


def test_a_block_with_no_claims_is_never_near():
    assert not text_near_claim(block("Plain."), [], block("Plain, changed."), [])


def test_a_span_without_a_claim_row_is_not_a_claim_unit():
    assert not near("[Revenue fell]{#c1}. Tail.", "[Revenue fell]{#c1}. Tail, changed.", [], [])


def test_whitespace_that_pandoc_ignores_is_not_a_change():
    assert not near("[Revenue fell 12%]{#c1}.   Tail.", "[Revenue fell 12%]{#c1}. Tail.")
