import httpx
import pytest
import respx

from pentice_quiz.errors import InvalidRequestError, SourceUnavailableError
from pentice_quiz.services.ingestion import (
    content_hash,
    extract_title,
    fetch_markdown,
    split_sections,
)

URL = "https://raw.example.com/rules.md"


class TestSplitSections:
    def test_bullet_document(self, harbor_rules: str) -> None:
        sections = split_sections(harbor_rules)
        assert [s.heading for s in sections] == [
            "Check-in and check-out",
            "Guests and visitors",
            "Noise",
            "Parking",
            "Smoking and pets",
            "Deposits and damages",
            "Trash and recycling",
            "Amenities",
            "Emergencies",
        ]
        assert sections[2].text.startswith("- Quiet hours are **10:00 PM to 8:00 AM**")

    def test_prose_document(self, pine_rules: str) -> None:
        sections = split_sections(pine_rules)
        assert [s.heading for s in sections] == [
            "Arrival",
            "Who can stay",
            "Quiet",
            "Pool",
            "Cars",
            "Pets",
            "Smoking",
            "Money",
            "Kitchen and laundry",
            "Waste",
            "Help",
        ]
        assert "two physical keys" in sections[0].text
        assert "Check-out is at **10:00 AM**" in sections[0].text

    def test_preamble_is_not_a_section(self, harbor_rules: str) -> None:
        assert all("river walk" not in s.text for s in split_sections(harbor_rules))

    def test_deeper_headings_stay_inside_their_section(self) -> None:
        sections = split_sections("## Parking\nGarage.\n### Bikes\nRack in B1.\n## Noise\nQuiet.")
        assert [s.heading for s in sections] == ["Parking", "Noise"]
        assert "### Bikes" in sections[0].text

    def test_headings_inside_code_fences_are_ignored(self) -> None:
        sections = split_sections("## Wifi\n```\n## not a heading\n```\nPassword on fridge.")
        assert len(sections) == 1

    def test_document_without_h2_is_one_section(self) -> None:
        sections = split_sections("# Tiny Flat\n\nNo parties. Quiet after 10 PM.")
        assert len(sections) == 1
        assert sections[0].heading == "Tiny Flat"
        assert sections[0].text == "No parties. Quiet after 10 PM."

    def test_empty_sections_are_dropped(self) -> None:
        assert [s.heading for s in split_sections("## Empty\n\n## Real\nText")] == ["Real"]

    def test_title(self, pine_rules: str) -> None:
        assert extract_title(pine_rules) == "Pine Court Residences — Guest Guide and Rules"
        assert extract_title("no title") is None


class TestFetch:
    @respx.mock
    def test_success(self) -> None:
        respx.get(URL).respond(
            200,
            text="# T\n## A\nText",
            headers={"content-type": "text/plain; charset=utf-8", "etag": '"abc"'},
        )
        with httpx.Client() as client:
            fetched = fetch_markdown(client, URL, max_bytes=1000)
        assert fetched.content == "# T\n## A\nText"
        assert fetched.content_hash == content_hash("# T\n## A\nText")
        assert fetched.etag == '"abc"'

    @respx.mock
    def test_http_error_is_502(self) -> None:
        respx.get(URL).respond(404)
        with httpx.Client() as client, pytest.raises(SourceUnavailableError, match="404"):
            fetch_markdown(client, URL, max_bytes=1000)

    @respx.mock
    def test_timeout_is_502(self) -> None:
        respx.get(URL).mock(side_effect=httpx.ReadTimeout("slow"))
        with httpx.Client() as client, pytest.raises(SourceUnavailableError, match="timed out"):
            fetch_markdown(client, URL, max_bytes=1000)

    @respx.mock
    def test_connection_error_is_502(self) -> None:
        respx.get(URL).mock(side_effect=httpx.ConnectError("refused"))
        with httpx.Client() as client, pytest.raises(SourceUnavailableError, match="unreachable"):
            fetch_markdown(client, URL, max_bytes=1000)

    @respx.mock
    def test_html_page_is_rejected(self) -> None:
        respx.get(URL).respond(200, text="<html></html>", headers={"content-type": "text/html"})
        with httpx.Client() as client, pytest.raises(SourceUnavailableError) as error:
            fetch_markdown(client, URL, max_bytes=1000)
        assert error.value.code == "source_invalid"

    @respx.mock
    def test_body_over_limit_is_rejected(self) -> None:
        respx.get(URL).respond(200, text="x" * 2000, headers={"content-type": "text/plain"})
        with httpx.Client() as client, pytest.raises(SourceUnavailableError) as error:
            fetch_markdown(client, URL, max_bytes=1000)
        assert error.value.code == "source_too_large"

    @respx.mock
    def test_invalid_utf8_is_rejected(self) -> None:
        respx.get(URL).respond(200, content=b"\xff\xfe\x00bad", headers={"content-type": ""})
        with httpx.Client() as client, pytest.raises(SourceUnavailableError, match="UTF-8"):
            fetch_markdown(client, URL, max_bytes=1000)

    @respx.mock
    def test_empty_body_is_rejected(self) -> None:
        respx.get(URL).respond(200, text="  \n", headers={"content-type": "text/markdown"})
        with httpx.Client() as client, pytest.raises(SourceUnavailableError, match="empty"):
            fetch_markdown(client, URL, max_bytes=1000)

    def test_non_http_scheme_is_rejected(self) -> None:
        with httpx.Client() as client, pytest.raises(InvalidRequestError):
            fetch_markdown(client, "file:///etc/passwd", max_bytes=1000)
