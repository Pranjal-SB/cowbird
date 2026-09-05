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


def test_extract_links_does_not_double_unescape_href_values():
    # HTMLParser already unescapes attributes. A second unescape can inject
    # metacharacters into a URL that the caller assumes is escaped.
    html = '<a href="https://a.test?q=&amp;amp;lt;b&amp;amp;gt;">x</a>'
    assert extract_links(html) == ("https://a.test?q=&amp;lt;b&amp;gt;",)


def test_extract_links_matches_schemes_case_insensitively():
    # RFC 3986 makes URL schemes case-insensitive. Real mail contains uppercase.
    # Original case is preserved in the output.
    assert extract_links('<a href="HTTPS://ok.test">x</a>') == ("HTTPS://ok.test",)
    assert extract_links('<a href="HttP://ok.test">x</a>') == ("HttP://ok.test",)
    assert extract_links(
        '<a href="HTTPS://a.test">x</a><a href="http://b.test">y</a>'
    ) == ("HTTPS://a.test", "http://b.test")


def test_dangerous_schemes_are_never_returned():
    # Schemes must always be filtered, regardless of case or encoding.
    # A single false negative here leaks attacker-controlled URLs to callers.
    dangerous = [
        '<a href="javascript:alert(1)">x</a>',
        '<a href="JavaScript:alert(1)">x</a>',
        '<a href="&#106;avascript:alert(1)">x</a>',  # HTML-entity encoded 'j'
        '<a href="//evil.test/x">x</a>',
        '<a href="data:text/html,x">x</a>',
        '<a href="\x01javascript:alert(1)">x</a>',  # Control char prefix
    ]
    for html in dangerous:
        assert extract_links(html) == (), f"Failed to block: {html!r}"


def test_unclosed_script_swallows_the_rest_like_a_browser_does():
    # This is stdlib HTMLParser CDATA mode matching browser behaviour.
    # We are keeping this behavior deliberately, not fixing it.
    html = "<script>evil<p>448213</p>"
    assert html_to_text(html) == ""
