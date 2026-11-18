"""anonbox.net parsing tests.

The /en page hides decoy address fragments in invisible elements and the mbox
format has its own escaping rules — both are this adapter's core skill, so they
get focused tests rather than living inside the contract suite.
"""

from pathlib import Path

import pytest
from cowbird.errors import SchemaDrift
from cowbird_anonbox import parse_mbox, parse_page

FIXTURES = Path(__file__).parent / "fixtures"


def page(name: str) -> str:
    return (FIXTURES / name).read_text()


class TestPageParsing:
    async def test_visible_address_is_extracted_with_decoys_removed(self):
        # page1 hides a whole decoy <dd> (…-test-1@5at52.googlemail.com) and
        # injects decoy fragments (-test2-, nixda, blumm) into display:none
        # spans inside the real line. The address must come from visible text
        # only.
        address, path = parse_page(page("page1.html"))
        assert address == "bkl4xd4atw@5at52.anonbox.net"
        assert path == "5at52/b3vb33iy4q"

    async def test_second_recorded_page_parses_to_its_own_address(self):
        address, path = parse_page(page("page2.html"))
        assert address == "bpk5qp744i@5at52.anonbox.net"
        assert path == "5at52/nhpxxoritz"

    async def test_decoy_googlemail_address_never_leaks(self):
        address, _ = parse_page(page("page1.html"))
        assert "googlemail" not in address

    async def test_missing_address_is_schema_drift(self):
        with pytest.raises(SchemaDrift):
            parse_page("<html><body><p>no address here</p></body></html>")

    async def test_missing_mbox_link_is_schema_drift(self):
        with pytest.raises(SchemaDrift):
            parse_page(
                "<html><body><dd><p>someone@5at52.anonbox.net</p></dd></body></html>"
            )


class TestMboxParsing:
    async def test_empty_mbox_is_no_messages(self):
        assert parse_mbox("") == []

    async def test_one_message_yields_headers_and_body(self):
        mbox = (
            "From noreply@example.test Fri Nov 14 12:00:00 2026\n"
            "From: noreply@example.test\n"
            "Subject: Your verification code is 482913.\n"
            "Message-ID: <abc@example.test>\n"
            "Date: Fri, 14 Nov 2026 12:00:00 +0000\n"
            "\n"
            "Your verification code is 482913.\n"
        )
        msgs = parse_mbox(mbox)
        assert len(msgs) == 1
        assert msgs[0].sender == "noreply@example.test"
        assert msgs[0].subject == "Your verification code is 482913."
        assert msgs[0].body == "Your verification code is 482913.\n"
        assert msgs[0].message_id == "<abc@example.test>"

    async def test_from_line_escaping_is_unescaped_in_the_body(self):
        # mboxo escaping: a body line starting with "From " is stored as
        # ">From ". The parser must strip exactly one level; an UNescaped
        # "From " line is a message separator, not body text.
        mbox = (
            "From noreply@example.test Fri Nov 14 12:00:00 2026\n"
            "From: noreply@example.test\n"
            "Subject: hi\n"
            "\n"
            ">From the archive:\n"
            ">>From deeper nesting\n"
            "From noreply@example.test Fri Nov 14 12:01:00 2026\n"
            "From: noreply@example.test\n"
            "Subject: two\n"
            "\n"
            "body two\n"
        )
        msgs = parse_mbox(mbox)
        assert len(msgs) == 2
        assert msgs[0].body.startswith("From the archive:\n")
        assert ">>From deeper nesting" in msgs[0].body

    async def test_multiple_messages_split_on_from_lines(self):
        mbox = (
            "From a@example.test Fri Nov 14 12:00:00 2026\n"
            "From: a@example.test\n"
            "Subject: one\n"
            "\n"
            "body one\n"
            "From b@example.test Fri Nov 14 12:01:00 2026\n"
            "From: b@example.test\n"
            "Subject: two\n"
            "\n"
            "body two\n"
        )
        msgs = parse_mbox(mbox)
        assert [m.sender for m in msgs] == ["a@example.test", "b@example.test"]
        assert [m.subject for m in msgs] == ["one", "two"]

    async def test_header_continuation_lines_are_unfolded(self):
        mbox = (
            "From noreply@example.test Fri Nov 14 12:00:00 2026\n"
            "From: noreply@example.test\n"
            "Subject: long\n"
            " subject line\n"
            "\n"
            "body\n"
        )
        msgs = parse_mbox(mbox)
        assert msgs[0].subject == "long subject line"

    async def test_missing_from_header_is_schema_drift(self):
        mbox = (
            "From noreply@example.test Fri Nov 14 12:00:00 2026\n"
            "Subject: no From header\n"
            "\n"
            "body\n"
        )
        with pytest.raises(SchemaDrift):
            parse_mbox(mbox)

    async def test_id_is_stable_across_reparse(self):
        # list() re-fetches the mbox on every call; ids must not change.
        mbox = (
            "From noreply@example.test Fri Nov 14 12:00:00 2026\n"
            "From: noreply@example.test\n"
            "Subject: hi\n"
            "\n"
            "body\n"
        )
        assert [m.id for m in parse_mbox(mbox)] == [
            m.id for m in parse_mbox(mbox)
        ]

    async def test_unknown_body_charset_degrades_instead_of_crashing(self):
        # A mislabeled/exotic charset (ks_c_5601-1987 is a real-world label
        # Python's codec registry rejects) must not raise: one unreadable
        # message must not kill every poll of the address.
        mbox = (
            "From noreply@example.test Fri Nov 14 12:00:00 2026\n"
            "From: noreply@example.test\n"
            "Subject: hi\n"
            'Content-Type: text/plain; charset="ks_c_5601-1987"\n'
            "\n"
            "body\n"
        )
        msgs = parse_mbox(mbox)
        assert len(msgs) == 1
        assert "body" in msgs[0].body

    async def test_unknown_charset_in_sender_encoded_word_falls_back(self):
        mbox = (
            "From noreply@example.test Fri Nov 14 12:00:00 2026\n"
            "From: =?x-unknown-charset?q?ab?= <noreply@example.test>\n"
            "Subject: hi\n"
            "\n"
            "body\n"
        )
        msgs = parse_mbox(mbox)
        assert msgs[0].sender == "noreply@example.test"
