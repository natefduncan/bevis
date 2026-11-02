"""Claims in the block editor, driven in a browser: make, edit, move, unlink, delete (SPEC §6.2)."""

from playwright.sync_api import expect

B2 = "[Revenue fell 12.0%, from $4.21M to $3.71M]{#c1}. The decline was broad, but not even: one segment accounts for most of it."
PLAIN = "Over the year 142 accounts churned, and we kept the rest."


def with_plain_block(seed):
    """An uncovered number in a block of its own: the paragraph to make a claim in."""
    seed.call("agent", "POST", f"/api/reports/{seed.rid}/edits",
              {"ops": [{"op": "upsert_block", "block": {"id": "b9", "type": "text", "text": PLAIN}}]})


def open_editor(page, seed, who, block):
    page.goto(seed.view(who, v="draft"))
    expect(page.locator("#meta")).to_contain_text("Draft, revision")
    page.locator("#edit-all").click()
    field = page.locator(f"#{'blk-' + block} .edit-field")
    expect(field).to_be_visible()
    return field


def select_text(page, block, text):
    """Select `text` inside the block's field, the way a mouse would, so the bar under it offers to make a claim."""
    page.evaluate("""([block, text]) => {
      const field = document.querySelector(`#blk-${block} .edit-field`);
      const walker = document.createTreeWalker(field, NodeFilter.SHOW_TEXT);
      for (let n = walker.nextNode(); n; n = walker.nextNode()) {
        const at = n.nodeValue.indexOf(text);
        if (at < 0) continue;
        const r = document.createRange();
        r.setStart(n, at); r.setEnd(n, at + text.length);
        const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r);
        field.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
        return;
      }
      throw new Error("no such text: " + text);
    }""", [block, text])


def field_markdown(page, block):
    return page.evaluate("""(block) => [...document.querySelector(`#blk-${block} .edit-field`).childNodes]
      .map((n) => n.nodeType === 3 ? n.nodeValue : (n.dataset?.src ?? "")).join("")""", block)


def claim(seed, cid):
    report = seed.call("agent", "GET", f"/api/reports/{seed.rid}")
    return next((c for c in report["claims"] if c["id"] == cid), None)


def start_claim(page, seed, text):
    with_plain_block(seed)
    open_editor(page, seed, "eli", "b9")
    select_text(page, "b9", text)
    page.get_by_role("button", name="Make this a claim").click()
    expect(page.locator("#blk-b9 .claim-composer")).to_contain_text("Claim c8")


def pick_churn_cell(page):
    box = page.locator("#blk-b9 .claim-composer")
    box.get_by_label("Evidence", exact=True).select_option("q-churn")
    box.get_by_label("Column").select_option("churned_accounts")
    box.get_by_label("Row", exact=True).select_option(index=0)  # the one row: this evidence has no key
    expect(box).to_contain_text("Cell: q-churn v1")


def test_making_a_claim_from_selected_text(page, seed):
    start_claim(page, seed, "142 accounts")
    box = page.locator("#blk-b9 .claim-composer")
    expect(box.get_by_label("Number as written")).to_have_value("142")  # found in the selection
    expect(page.locator("#edit-save")).to_be_disabled()  # a number is not filled in yet
    expect(page.locator("#blk-b9 .edit-status")).to_contain_text("Finish claim c8")

    pick_churn_cell(page)
    expect(box.locator(".cmp-result")).to_contain_text("verified")  # the validator checked it live
    box.get_by_role("button", name="Done").click()
    expect(page.locator("#blk-b9 .claim-chip")).to_have_text("142 accountsc8")

    page.locator("#edit-save").click()
    expect(page.locator("#edit-summary")).to_contain_text("Saved 1 block")
    saved = claim(seed, "c8")
    assert saved["block_id"] == "b9" and saved["numbers"][0]["cell"]["column"] == "churned_accounts"
    assert saved["numbers"][0]["status"] == "ok"
    blocks = seed.call("agent", "GET", f"/api/reports/{seed.rid}")["blocks"]
    assert next(b for b in blocks if b["id"] == "b9")["text"].startswith("Over the year [142 accounts]{#c8} churned")


def test_cancelling_a_new_claim_leaves_plain_text(page, seed):
    start_claim(page, seed, "142 accounts")
    page.locator("#blk-b9 .claim-composer").get_by_role("button", name="Cancel, not a claim").click()
    expect(page.locator("#blk-b9 .claim-chip")).to_have_count(0)
    expect(page.locator("#edit-save")).to_be_disabled()  # back to what it was
    assert field_markdown(page, "b9") == PLAIN


def test_a_claim_cannot_be_made_across_brackets(page, seed):
    with_plain_block(seed)
    seed.call("agent", "POST", f"/api/reports/{seed.rid}/edits",
              {"ops": [{"op": "upsert_block", "block": {"id": "b9", "type": "text", "text": "See [note] about 142 accounts."}}]})
    open_editor(page, seed, "eli", "b9")
    select_text(page, "b9", "[note] about 142")
    page.get_by_role("button", name="Make this a claim").click()
    expect(page.locator("#blk-b9 .edit-status")).to_contain_text("cannot hold brackets")
    expect(page.locator("#blk-b9 .claim-chip")).to_have_count(0)


def test_unlinking_a_claim_keeps_its_text(page, seed):
    open_editor(page, seed, "eli", "b2")
    page.locator("#blk-b2 .claim-chip").click()
    page.get_by_role("button", name="Unlink, keep the text").click()
    expect(page.locator("#blk-b2 .claim-chip")).to_have_count(0)
    page.locator("#edit-save").click()
    expect(page.locator("#edit-summary")).to_contain_text("Saved 1 block")
    assert claim(seed, "c1") is None
    text = next(b for b in seed.call("agent", "GET", f"/api/reports/{seed.rid}")["blocks"] if b["id"] == "b2")["text"]
    assert text.startswith("Revenue fell 12.0%, from $4.21M to $3.71M. The decline")


def test_deleting_a_claim_removes_its_text_too(page, seed):
    open_editor(page, seed, "eli", "b2")
    page.locator("#blk-b2 .claim-chip").click()
    page.get_by_role("button", name="Delete claim and text").click()
    assert field_markdown(page, "b2").startswith(". The decline was broad")
    page.locator("#edit-save").click()
    expect(page.locator("#edit-summary")).to_contain_text("Saved 1 block")
    expect(page.locator("#blk-b2")).not_to_contain_text("Revenue fell")
    assert claim(seed, "c1") is None


def test_a_chip_moves_over_a_word_with_alt_arrow(page, seed):
    open_editor(page, seed, "eli", "b2")
    chip = page.locator("#blk-b2 .claim-chip")
    save = page.locator("#edit-save")
    expect(save).to_be_disabled()
    chip.focus()
    page.keyboard.press("Alt+ArrowRight")
    assert field_markdown(page, "b2") == ".[Revenue fell 12.0%, from $4.21M to $3.71M]{#c1} The decline was broad, but not even: one segment accounts for most of it."
    expect(save).to_be_enabled()
    page.keyboard.press("Alt+ArrowLeft")  # and back: nothing is left to save
    assert field_markdown(page, "b2") == B2
    expect(save).to_be_disabled()


def test_a_chip_can_be_dragged_within_its_block(page, seed):
    open_editor(page, seed, "eli", "b2")
    field = page.locator("#blk-b2 .edit-field")
    chip = field.locator(".claim-chip")
    box = field.bounding_box()
    chip.drag_to(field, target_position={"x": box["width"] - 40, "y": 12})
    moved = field_markdown(page, "b2")
    assert moved != B2 and moved.count("[Revenue fell 12.0%, from $4.21M to $3.71M]{#c1}") == 1
    assert "{#c1}" in moved and not moved.startswith("[Revenue")  # the chip left the start, whole
    expect(page.locator("#edit-save")).to_be_enabled()


def test_editing_a_claim_and_cancelling_changes_nothing(page, seed):
    open_editor(page, seed, "eli", "b2")
    page.locator("#blk-b2 .claim-chip").click()
    page.get_by_role("button", name="Edit claim").click()
    box = page.locator("#blk-b2 .claim-composer")
    expect(box).to_contain_text("Claim c1")
    expect(box.get_by_label("Number as written")).to_have_count(3)  # 12.0%, $4.21M, $3.71M
    box.get_by_role("button", name="Remove").first.click()
    expect(box.get_by_label("Number as written")).to_have_count(2)
    box.get_by_role("button", name="Cancel", exact=True).click()
    expect(page.locator("#blk-b2 .claim-composer")).to_be_empty()
    expect(page.locator("#edit-save")).to_be_disabled()
    page.locator("#blk-b2 .claim-chip").click()
    page.get_by_role("button", name="Edit claim").click()
    expect(page.locator("#blk-b2 .claim-composer").get_by_label("Number as written")).to_have_count(3)  # still there


def test_the_textarea_fallback_edits_markdown_and_keeps_claims(browser, seed):
    ctx = browser.new_context(viewport={"width": 1200, "height": 900})
    page = ctx.new_page()
    page.set_default_timeout(8000)
    # A browser without contenteditable="plaintext-only": the editor is a textarea of Markdown.
    page.add_init_script("""Object.defineProperty(HTMLElement.prototype, "contentEditable",
      { configurable: true, get() { return "inherit"; }, set() {} });""")
    try:
        page.goto(seed.view("eli", v="draft"))
        page.locator("#edit-all").click()
        field = page.locator("#blk-b2 textarea.edit-field")
        expect(field).to_have_value(B2)  # the claim is shown as its Markdown source
        field.focus()
        page.keyboard.press("Control+End")
        page.keyboard.type(" Margins held.")
        page.locator("#edit-save").click()
        expect(page.locator("#edit-summary")).to_contain_text("Saved 1 block")
        expect(field).to_have_value(B2 + " Margins held.")
        saved = next(b for b in seed.call("agent", "GET", f"/api/reports/{seed.rid}")["blocks"] if b["id"] == "b2")
        assert saved["text"] == B2 + " Margins held."
        assert claim(seed, "c1") is not None  # the claim span was left alone
        # It cannot make claims here: no bar to select text for one.
        expect(page.get_by_role("button", name="Make this a claim")).to_have_count(0)
        expect(page.locator("#blk-b2 .claim-chip")).to_have_count(0)
    finally:
        ctx.close()


def get_report(seed):
    return seed.call("agent", "GET", f"/api/reports/{seed.rid}")


def start_whole_report_edit(page, seed, who="eli"):
    page.goto(seed.view(who, v="draft"))
    expect(page.locator("#meta")).to_contain_text("Draft, revision")
    page.locator("#edit-all").click()
    expect(page.locator("#blk-b4 .claim-chip[data-claim='c4']")).to_be_visible()


def test_a_claim_moves_to_another_block_with_the_picker(page, seed):
    before = next(c for c in get_report(seed)["claims"] if c["id"] == "c4")
    start_whole_report_edit(page, seed)
    page.locator("#blk-b4 .claim-chip[data-claim='c4']").click()
    page.get_by_label("Move claim to block").select_option("b2")
    expect(page.locator("#blk-b2 .claim-chip[data-claim='c4']")).to_be_visible()
    expect(page.locator("#blk-b4 .claim-chip[data-claim='c4']")).to_have_count(0)
    expect(page.locator("#blk-b4 .claim-chip")).to_have_count(2)  # c2 and c3 stay
    expect(page.locator("#edit-summary")).to_have_text("2 blocks changed")
    # Both blocks are checked together, so neither reports the span as dropped or in the wrong block.
    expect(page.locator("#blk-b2 .edit-status")).to_contain_text("no problems")
    expect(page.locator("#blk-b4 .edit-status")).not_to_contain_text("error")
    expect(page.locator("#edit-save")).to_be_enabled()

    page.locator("#edit-save").click()
    expect(page.locator("#edit-summary")).to_contain_text("Saved 2 blocks")
    after = get_report(seed)
    moved = next(c for c in after["claims"] if c["id"] == "c4")
    assert moved["block_id"] == "b2"
    assert moved["evidence"] == before["evidence"] and moved["numbers"][0]["cell"] == before["numbers"][0]["cell"]
    assert moved["numbers"][0]["status"] == "ok"
    texts = {b["id"]: b.get("text", "") for b in after["blocks"]}
    assert "{#c4}" in texts["b2"] and "{#c4}" not in texts["b4"]
    assert texts["b2"].endswith("with them]{#c4}")  # at the end of the block it moved to


def test_a_claim_is_dragged_into_another_block(page, seed):
    start_whole_report_edit(page, seed)
    chip = page.locator("#blk-b4 .claim-chip[data-claim='c4']")
    target = page.locator("#blk-b2 .edit-field")
    box = target.bounding_box()
    chip.drag_to(target, target_position={"x": 30, "y": min(box["height"] - 6, 40)})
    expect(page.locator("#blk-b2 .claim-chip[data-claim='c4']")).to_be_visible()
    expect(page.locator("#blk-b4 .claim-chip[data-claim='c4']")).to_have_count(0)
    expect(page.locator("#blk-b2 .edit-status")).to_contain_text("no problems")
    page.locator("#edit-save").click()
    expect(page.locator("#edit-summary")).to_contain_text("Saved 2 blocks")
    assert next(c for c in get_report(seed)["claims"] if c["id"] == "c4")["block_id"] == "b2"


def test_a_moved_claim_survives_a_reload_and_still_checks_as_a_pair(page, seed):
    start_whole_report_edit(page, seed)
    page.locator("#blk-b4 .claim-chip[data-claim='c4']").click()
    page.get_by_label("Move claim to block").select_option("b2")
    expect(page.locator("#edit-summary")).to_have_text("2 blocks changed")
    page.reload()
    page.locator("#edit-all").click()
    expect(page.locator("#blk-b2 .claim-chip[data-claim='c4']")).to_be_visible()  # the unsaved move is restored
    expect(page.locator("#edit-summary")).to_have_text("2 blocks changed")
    expect(page.locator("#blk-b2 .edit-status")).to_contain_text("no problems")
    expect(page.locator("#blk-b4 .edit-status")).not_to_contain_text("error")
    page.locator("#edit-save").click()
    expect(page.locator("#edit-summary")).to_contain_text("Saved 2 blocks")
    assert next(c for c in get_report(seed)["claims"] if c["id"] == "c4")["block_id"] == "b2"


def test_a_moved_claim_can_still_be_edited_and_unlinked_in_its_new_block(page, seed):
    start_whole_report_edit(page, seed)
    page.locator("#blk-b4 .claim-chip[data-claim='c4']").click()
    page.get_by_label("Move claim to block").select_option("b2")
    chip = page.locator("#blk-b2 .claim-chip[data-claim='c4']")
    chip.click()  # the chip answers to the block it is in now
    expect(page.locator("#blk-b2 .claim-tools")).to_contain_text("Claim c4:")
    expect(page.locator("#blk-b4 .claim-tools")).not_to_contain_text("Claim c4")
    page.get_by_role("button", name="Unlink, keep the text").click()
    page.locator("#edit-save").click()
    expect(page.locator("#edit-summary")).to_contain_text("Saved 2 blocks")
    assert not any(c["id"] == "c4" for c in get_report(seed)["claims"])
    b2 = next(b for b in get_report(seed)["blocks"] if b["id"] == "b2")
    assert "142 accounts churned" in b2["text"] and "{#c4}" not in b2["text"]
