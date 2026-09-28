import pytest
from playwright.sync_api import sync_playwright
from signup031.manual_replay import _locate


@pytest.fixture
def page():
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(headless=True)
        context=browser.new_context(offline=True)
        yield context.new_page()
        browser.close()


def test_delayed_exact_target_waits_without_selector_fallback(page):
    page.set_content("<div id='container'></div><script>setTimeout(()=>{document.querySelector('#container').innerHTML='<button id=delayed>Ready</button>'},250)</script>")
    row={'selector':'#delayed','signature':{'tag':'BUTTON','type':'','name':''}}
    assert _locate(page,row).inner_text()=='Ready'


def test_duplicate_and_changed_identity_still_fail(page):
    row={'selector':'.target','signature':{'tag':'BUTTON','type':'','name':''}}
    page.set_content('<button class=target>A</button><button class=target>B</button>')
    with pytest.raises(ValueError,match='missing or ambiguous'):_locate(page,row)
    page.set_content('<div class=target>wrong identity</div>')
    with pytest.raises(ValueError,match='identity changed'):_locate(page,row)


def test_missing_target_reports_bounded_wait(page):
    page.set_content('<button id=other>Do not substitute</button>')
    row={'selector':'#missing','signature':{'tag':'BUTTON','type':'','name':''}}
    with pytest.raises(ValueError,match='100 ms'):_locate(page,row,timeout=100)
