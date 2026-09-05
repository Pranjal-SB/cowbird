from cowbird.parsing import extract_links, extract_otp, html_to_text


def test_html_to_text_drops_script_and_style_bodies():
    html = "<style>a{color:red}</style><script>alert(1)</script><p>Hi there</p>"
    assert html_to_text(html) == "Hi there"


def test_extract_links_returns_hrefs_in_order_without_duplicates():
    html = '<a href="https://a.test/1">x</a><a href="https://a.test/1">y</a><a href="https://b.test">z</a>'
    assert extract_links(html) == ("https://a.test/1", "https://b.test")


def test_extract_links_ignores_non_http_schemes():
    # Message HTML is attacker-controlled; javascript: and data: must never surface.
    html = '<a href="javascript:alert(1)">x</a><a href="https://ok.test">y</a>'
    assert extract_links(html) == ("https://ok.test",)


def test_extract_otp_prefers_a_standalone_six_digit_code():
    assert extract_otp("Your code is 448213. Ref 99281734 ignored.") == "448213"


def test_extract_otp_falls_back_to_any_four_to_eight_digit_run():
    assert extract_otp("PIN: 9182") == "9182"


def test_extract_otp_returns_none_when_there_is_no_code():
    assert extract_otp("Welcome to the service, no numbers here") is None


def test_extract_otp_honours_an_explicit_pattern():
    assert extract_otp("token ABC-123 here", pattern=r"[A-Z]{3}-\d{3}") == "ABC-123"
