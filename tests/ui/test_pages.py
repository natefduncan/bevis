"""The web view driven in a browser: index and inbox, editing, review, publishing, sharing,
suggestions, and the live conflict notice."""

import re

from playwright.sync_api import expect

B2 = "[Revenue fell 12.0%, from $4.21M to $3.71M]{#c1}. The decline was broad, but not even: one segment accounts for most of it."
EDIT_B2 = {"ops": [{"op": "upsert_block", "block": {"id": "b2", "type": "text", "text": B2.replace("broad", "wide")}}]}


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
    page.get_by_label("Edit paragraph b2").click()
    field = page.locator("#blk-b2 .edit-field")
    field.focus()
    page.keyboard.press("Control+End")
    page.keyboard.type(" Margins held.")
    save = page.locator("#blk-b2 .edit-save")
    expect(save).to_be_enabled()
    save.click()
    expect(page.locator("#blk-b2").locator("xpath=..").locator(".flash")).to_contain_text("admin has to review")
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


def test_a_change_to_the_open_block_shows_a_notice_without_typing(page, seed):
    page.clock.install()
    open_draft(page, seed, "ada")
    page.get_by_label("Edit paragraph b2").click()
    expect(page.locator("#blk-b2 .edit-field")).to_be_visible()
    expect(page.locator("#blk-b2 .edit-conflict")).to_be_empty()
    edit_b2_as(seed, "eli")
    page.clock.run_for(11_000)  # one poll
    notice = page.locator("#blk-b2 .edit-conflict")
    expect(notice).to_contain_text("This block changed while you were editing it")
    expect(notice.get_by_role("button", name="Use current text")).to_be_visible()
    notice.get_by_role("button", name="Use current text").click()
    expect(page.locator("#blk-b2 .edit-field")).to_contain_text("wide")
    expect(page.locator("#blk-b2 .edit-conflict")).to_be_empty()


def test_a_suggester_suggests_and_an_editor_accepts_without_a_flag(page, seed):
    open_draft(page, seed, "sue")
    page.get_by_label("Suggest an edit to paragraph b2").click()
    page.locator("#blk-b2 .edit-field").focus()
    page.keyboard.press("Control+End")
    page.keyboard.type(" Margins held.")
    page.locator("#blk-b2 .edit-save").click()
    expect(page.locator("#suggestions")).to_contain_text("Suggestions (1)")

    open_draft(page, seed, "eli")
    page.locator("#suggestions").click()
    page.locator("#panel-body").get_by_role("button").first.click()  # the one suggestion
    page.get_by_role("button", name="Accept").click()
    expect(page.locator("#blk-b2")).to_contain_text("Margins held.")
    expect(page.locator("#meta .chip-button")).to_have_count(0)  # accepting is a review; nothing is flagged


def test_typing_a_in_the_editor_does_not_toggle_audit_mode(page, seed):
    open_draft(page, seed, "eli")
    page.get_by_label("Edit paragraph b2").click()
    page.locator("#blk-b2 .edit-field").focus()
    page.keyboard.press("Control+End")
    page.keyboard.type(" a banana")
    expect(page.locator("#audit")).to_have_attribute("aria-pressed", "false")
    expect(page.locator("#blk-b2 .edit-field")).to_contain_text("it. a banana")  # focus was kept to the end


def test_changing_the_version_in_the_address_opens_it(page, seed):
    open_draft(page, seed, "eli")
    page.evaluate(f"location.hash = location.hash.replace('v=draft', 'v=1')")
    expect(page.locator("#meta")).to_contain_text("Version 1")
