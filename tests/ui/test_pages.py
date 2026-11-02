"""The web view driven in a browser: index and inbox, editing, review, publishing, sharing,
suggestions, and the live conflict notice."""

import re

from playwright.sync_api import expect

B2 = "[Revenue fell 12.0%, from $4.21M to $3.71M]{#c1}. The decline was broad, but not even: one segment accounts for most of it."
EDIT_B2 = {"ops": [{"op": "upsert_block", "block": {"id": "b2", "type": "text", "text": B2.replace("broad", "wide")}}]}


def poll_until(page, check, tries=6):
    """Run the page's 10-second poll (the clock is faked) until `check` holds: a slow response costs another cycle."""
    for _ in range(tries - 1):
        page.clock.run_for(11_000)
        try:
            check(timeout=1500)
            return
        except AssertionError:
            pass
    page.clock.run_for(11_000)
    check()


def edit_b2_as(seed, who):
    seed.call(who, "POST", f"/api/reports/{seed.rid}/edits", EDIT_B2)


def open_draft(page, seed, who):
    page.goto(seed.view(who, v="draft"))
    expect(page.locator("#meta")).to_contain_text("Draft, revision")


def test_the_index_and_inbox_show_what_needs_me(page, seed):
    edit_b2_as(seed, "eli")  # an editor's edit near a claim flags b2 for the admin
    page.goto(seed.index("ada"))
    row = page.locator("table.index tbody tr")
    expect(row).to_have_count(1)
    expect(row).to_contain_text("admin")
    expect(row).to_contain_text("1 block to review")
    expect(row).to_contain_text("draft ahead")

    page.get_by_role("tab", name=re.compile("Inbox")).click()
    item = page.locator("ul.inbox li")
    expect(item).to_have_count(1)
    expect(item).to_contain_text("Review")
    item.locator("a").click()  # lands on the block's review panel
    expect(page.locator("#panel-title")).to_be_visible()
    expect(page.get_by_role("button", name="Mark reviewed")).to_be_visible()


def test_an_edit_near_a_claim_is_flagged_and_blocks_publishing_until_reviewed(page, seed):
    open_draft(page, seed, "eli")
    page.locator("#edit-all").click()
    field = page.locator("#blk-b2 .edit-field")
    field.focus()
    page.keyboard.press("Control+End")
    page.keyboard.type(" Margins held.")
    save = page.locator("#edit-save")
    expect(save).to_be_enabled()
    save.click()
    expect(page.locator("#edit-summary")).to_contain_text("needs an admin's review")
    flag = page.locator("#meta .chip-button")
    expect(flag).to_contain_text("1 block to review")

    page.locator("#publish").click()
    expect(page.locator("#panel-body")).to_contain_text("1 block must be reviewed first")
    expect(page.locator("#panel-body button.primary", has_text="Publish")).to_be_disabled()

    # An admin marks it reviewed; then publishing goes through and the view moves to the new version.
    page.goto(seed.view("ada", v="draft", rv="b2"))
    page.get_by_role("button", name="Mark reviewed").click()
    expect(page.locator("#meta .chip-button")).to_have_count(0)
    page.locator("#publish").click()
    expect(page.locator("#panel-body")).to_contain_text("as version 2")
    page.locator("#panel-body button.primary", has_text="Publish").click()
    expect(page.locator("#meta")).to_contain_text("Version 2")
    expect(page.locator("#publish")).to_be_hidden()


def test_publishing_with_errors_needs_a_reason(page, seed):
    bad = {"ops": [{"op": "upsert_block", "block": {"id": "b2", "type": "text", "text": B2.replace("12.0%", "15.0%")}}]}
    seed.call("ada", "POST", f"/api/reports/{seed.rid}/edits", bad)
    seed.call("ada", "POST", f"/api/reports/{seed.rid}/blocks/b2/clear_review", {"revision": 2})
    open_draft(page, seed, "ada")
    page.locator("#publish").click()
    go = page.locator("#panel-body button.primary", has_text="Publish")
    expect(page.locator("#panel-body")).to_contain_text("2 errors")
    expect(go).to_be_disabled()
    page.get_by_label("Reason").fill("Number correction pending")
    expect(go).to_be_enabled()
    go.click()
    expect(page.locator("#meta")).to_contain_text("published with override")


def test_the_publish_button_is_for_editors_on_the_draft(page, seed):
    open_draft(page, seed, "sue")
    expect(page.locator("#publish")).to_be_hidden()  # a suggester cannot publish
    open_draft(page, seed, "eli")
    expect(page.locator("#publish")).to_be_visible()
    page.goto(seed.view("eli", v="1"))
    expect(page.locator("#meta")).to_contain_text("Version 1")
    expect(page.locator("#publish")).to_be_hidden()  # a published version is not published again


def test_members_are_listed_and_a_shared_link_removes_everyone_on_it(page, seed):
    link = seed.call("ada", "POST", f"/api/reports/{seed.rid}/links",
                     {"name": "Finance", "role": "commenter", "disclosure": "cited"}, 201)["secret"]
    for who in ("eli", "sue"):
        seed.call(who, "POST", "/api/me/claims", {"link": link})
    open_draft(page, seed, "ada")
    page.locator("#share").click()
    members = page.locator("table.members tbody tr")
    expect(members).to_have_count(5)  # ada, eli, sue, and eli and sue again on the shared link
    expect(page.locator("table.members")).to_contain_text("link shared with 1 other")
    expect(members.filter(has_text="you")).to_have_count(1)

    page.once("dialog", lambda d: (assert_has(d.message, "lose that link too"), d.accept()))
    members.filter(has_text=re.compile(r"^sue\d+ \(link shared")).get_by_role("button", name="Remove").click()
    expect(page.locator("#panel-body .message")).to_contain_text("lost that link too")
    # Sue lost every link; Eli kept only his direct one.
    expect(page.locator("table.members tbody tr")).to_have_count(2)
    expect(page.locator("table.links tr.revoked")).to_have_count(2)


def assert_has(message, text):
    assert text in message, message


def test_a_change_to_a_block_you_changed_shows_a_notice_without_typing(page, seed):
    page.clock.install()
    open_draft(page, seed, "ada")
    page.locator("#edit-all").click()
    field = page.locator("#blk-b2 .edit-field")
    field.focus()
    page.keyboard.press("Control+End")
    page.keyboard.type(" Mine.")
    expect(page.locator("#blk-b2 .edit-conflict")).to_be_empty()
    edit_b2_as(seed, "eli")
    notice = page.locator("#blk-b2 .edit-conflict")
    poll_until(page, lambda **kw: expect(notice).to_contain_text("This block changed", **kw))
    expect(notice).to_contain_text("This block changed while you were editing it")
    expect(page.locator("#edit-save")).to_be_disabled()  # saving waits until you choose
    expect(page.locator("#edit-summary")).to_contain_text("Block b2 changed")
    notice.get_by_role("button", name="Merge").click()
    expect(page.locator("#blk-b2 .edit-conflict")).to_be_empty()
    expect(field).to_contain_text("wide")  # their change
    expect(page.locator("#blk-b2 .edit-leftover")).to_contain_text("Mine.")  # we clashed: mine is kept aside to copy from
    expect(page.locator("#edit-save")).to_be_disabled()  # what is left is theirs: nothing of mine to save


def test_a_block_you_have_not_changed_follows_other_edits(page, seed):
    page.clock.install()
    open_draft(page, seed, "ada")
    page.locator("#edit-all").click()
    expect(page.locator("#blk-b2 .edit-field")).to_contain_text("broad")
    edit_b2_as(seed, "eli")
    poll_until(page, lambda **kw: expect(page.locator("#blk-b2 .edit-field")).to_contain_text("wide", **kw))
    # no notice: nothing of yours to lose
    expect(page.locator("#blk-b2 .edit-conflict")).to_be_empty()
    expect(page.locator("#edit-save")).to_be_disabled()


def test_a_suggester_suggests_and_an_editor_accepts_without_a_flag(page, seed):
    open_draft(page, seed, "sue")
    page.locator("#edit-all").click()
    page.locator("#blk-b2 .edit-field").focus()
    page.keyboard.press("Control+End")
    page.keyboard.type(" Margins held.")
    expect(page.locator("#edit-title")).to_have_text("Suggesting edits")
    page.locator("#edit-save").click()
    expect(page.locator("#edit-summary")).to_contain_text("Suggested 1 block")
    page.locator("#edit-done").click()
    expect(page.locator("#suggestions")).to_contain_text("Suggestions (1)")

    open_draft(page, seed, "eli")
    page.locator("#suggestions").click()
    page.locator("#panel-body").get_by_role("button").first.click()  # the one suggestion
    page.get_by_role("button", name="Accept").click()
    expect(page.locator("#blk-b2")).to_contain_text("Margins held.")
    expect(page.locator("#meta .chip-button")).to_have_count(0)  # accepting is a review; nothing is flagged


def test_typing_a_in_the_editor_does_not_toggle_audit_mode(page, seed):
    open_draft(page, seed, "eli")
    page.locator("#edit-all").click()
    page.locator("#blk-b2 .edit-field").focus()
    page.keyboard.press("Control+End")
    page.keyboard.type(" a banana")
    expect(page.locator("#audit")).to_have_attribute("aria-pressed", "false")
    expect(page.locator("#blk-b2 .edit-field")).to_contain_text("it. a banana")  # focus was kept to the end


def test_changing_the_version_in_the_address_opens_it(page, seed):
    open_draft(page, seed, "eli")
    page.evaluate(f"location.hash = location.hash.replace('v=draft', 'v=1')")
    expect(page.locator("#meta")).to_contain_text("Version 1")


def type_at_end(page, block, text):
    field = page.locator(f"#blk-{block} .edit-field")
    field.focus()
    page.keyboard.press("Control+End")
    page.keyboard.type(text)


def draft_text(seed, block):
    blocks = seed.call("agent", "GET", f"/api/reports/{seed.rid}")["blocks"]
    return next(b for b in blocks if b["id"] == block)["text"]


def draft_revision(seed):
    return seed.call("agent", "GET", f"/api/reports/{seed.rid}")["revision"]


def test_the_whole_report_opens_for_editing_and_saves_in_one_edit(page, seed):
    before = draft_revision(seed)
    open_draft(page, seed, "ada")
    expect(page.locator("#edit-bar")).to_be_hidden()
    page.locator("#edit-all").click()
    expect(page.locator("#edit-bar")).to_be_visible()
    expect(page.locator("#edit-all")).to_be_hidden()
    # Every text block and heading is open at once; charts are not text.
    expect(page.locator(".block-editor")).to_have_count(6)  # b1 to b4, b6 and b7
    expect(page.locator("#edit-summary")).to_have_text("No changes yet")
    expect(page.locator("#edit-save")).to_be_disabled()

    type_at_end(page, "b2", " Margins held.")
    type_at_end(page, "b7", " Watch next quarter.")
    expect(page.locator("#edit-summary")).to_have_text("2 blocks changed")
    page.locator("#edit-save").click()
    expect(page.locator("#edit-summary")).to_contain_text("Saved 2 blocks")
    assert draft_text(seed, "b2").endswith("Margins held.") and draft_text(seed, "b7").endswith("Watch next quarter.")
    assert draft_revision(seed) == before + 1  # one edit, not one per block
    expect(page.locator(".block-editor")).to_have_count(6)  # still editing, from the saved text
    expect(page.locator("#edit-save")).to_be_disabled()


def test_a_conflict_in_one_block_holds_back_the_whole_save(page, seed):
    page.clock.install()
    open_draft(page, seed, "ada")
    page.locator("#edit-all").click()
    type_at_end(page, "b2", " Mine.")
    type_at_end(page, "b7", " Watch next quarter.")
    edit_b2_as(seed, "eli")
    poll_until(page, lambda **kw: expect(page.locator("#blk-b2 .edit-conflict")).to_contain_text("changed while you were editing", **kw))
    expect(page.locator("#blk-b7 .edit-conflict")).to_be_empty()
    expect(page.locator("#edit-save")).to_be_disabled()
    page.locator("#blk-b2 .edit-conflict").get_by_role("button", name="Keep mine").click()
    expect(page.locator("#edit-save")).to_be_enabled()
    page.locator("#edit-save").click()
    expect(page.locator("#edit-summary")).to_contain_text("Saved 2 blocks")
    assert draft_text(seed, "b2").endswith("Mine.") and "wide" not in draft_text(seed, "b2")  # kept mine over theirs
    assert draft_text(seed, "b7").endswith("Watch next quarter.")


def test_a_stale_save_changes_nothing(page, seed):
    open_draft(page, seed, "ada")
    page.locator("#edit-all").click()
    type_at_end(page, "b2", " Mine.")
    type_at_end(page, "b7", " Watch next quarter.")
    edit_b2_as(seed, "eli")  # lands after we opened, before the next poll
    before = draft_revision(seed)
    page.locator("#edit-save").click()
    expect(page.locator("#blk-b2 .edit-conflict")).to_contain_text("changed while you were editing")
    assert draft_revision(seed) == before  # nothing was saved, not even b7
    assert not draft_text(seed, "b7").endswith("Watch next quarter.")
    expect(page.locator("#edit-save")).to_be_disabled()


def test_done_asks_before_discarding_and_discards_on_yes(page, seed):
    open_draft(page, seed, "ada")
    page.locator("#edit-all").click()
    type_at_end(page, "b2", " Margins held.")
    messages = []
    page.once("dialog", lambda d: (messages.append(d.message), d.dismiss()))
    page.locator("#edit-done").click()
    assert "1 block" in messages[0]
    expect(page.locator("#edit-bar")).to_be_visible()  # said no: still editing, text kept
    page.once("dialog", lambda d: d.accept())
    page.locator("#edit-done").click()
    expect(page.locator("#edit-bar")).to_be_hidden()
    expect(page.locator(".block-editor")).to_have_count(0)
    expect(page.locator("#edit-all")).to_be_visible()
    assert not draft_text(seed, "b2").endswith("Margins held.")


def test_done_with_no_changes_just_closes(page, seed):
    open_draft(page, seed, "eli")
    page.locator("#edit-all").click()
    page.locator("#edit-done").click()  # no dialog to answer
    expect(page.locator("#edit-bar")).to_be_hidden()


def test_the_edit_button_is_only_on_the_draft_for_suggesters_and_up(page, seed):
    open_draft(page, seed, "sue")
    expect(page.locator("#edit-all")).to_be_visible()
    page.goto(seed.view("sue", v="1"))
    expect(page.locator("#meta")).to_contain_text("Version 1")
    expect(page.locator("#edit-all")).to_be_hidden()
    link = seed.call("ada", "POST", f"/api/reports/{seed.rid}/links", {"name": "r", "role": "reader"}, 201)["secret"]
    page.goto(f"{seed.host.url}/view#key={link}")
    expect(page.locator("#meta")).to_contain_text("Version 1")
    expect(page.locator("#edit-all")).to_be_hidden()


def test_ctrl_enter_saves_from_any_block(page, seed):
    open_draft(page, seed, "eli")
    page.locator("#edit-all").click()
    type_at_end(page, "b7", " Watch next quarter.")
    page.keyboard.press("Control+Enter")
    expect(page.locator("#edit-summary")).to_contain_text("Saved 1 block")


def test_an_unsaved_edit_survives_a_reload(page, seed):
    open_draft(page, seed, "eli")
    page.locator("#edit-all").click()
    type_at_end(page, "b2", " Margins held.")
    page.reload()
    page.locator("#edit-all").click()
    expect(page.locator("#blk-b2 .edit-field")).to_contain_text("Margins held.")
    expect(page.locator("#blk-b2 .edit-status")).to_contain_text("Restored your unsaved text")
