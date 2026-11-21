"""Offline tests: no network, no API key needed."""

import pytest

from opener.core import OpenerError, Page, _clean_text, build_source, normalize_url, verify_draft

SOURCE = "TITLE: Acme Invoices\nSend invoices that get paid in 7 days.\nBuilt for freelance designers.\nFounded by Jane Doe."


def good_draft(**over):
    d = {
        "founder": "Jane Doe",
        "founder_evidence": "Founded by Jane Doe",
        "lines": [
            {"text": "Acme focuses on getting freelance designers paid faster.", "evidence": "Built for freelance designers"},
            {"text": "Seven-day payment is a bold promise to lead with.", "evidence": "get paid in 7 days"},
            {"text": "Is that promise what wins most of your customers?", "evidence": ""},
        ],
    }
    d.update(over)
    return d


def test_normalize_adds_scheme_and_lowercases():
    assert normalize_url("Example.COM") == "https://example.com/"
    assert normalize_url("http://foo.io/about") == "http://foo.io/about"


@pytest.mark.parametrize("bad", ["", "   ", "localhost", "not a url"])
def test_normalize_rejects_bad(bad):
    with pytest.raises(OpenerError):
        normalize_url(bad)


def test_verify_passes_good_draft():
    assert verify_draft(good_draft(), SOURCE) == []


def test_verify_catches_invented_evidence():
    d = good_draft()
    d["lines"][0]["evidence"] = "Trusted by 10,000 companies"
    problems = verify_draft(d, SOURCE)
    assert any("not found on the site" in p for p in problems)


def test_verify_catches_unbacked_founder():
    problems = verify_draft(good_draft(founder="John Smith"), SOURCE)
    assert any("Founder name" in p for p in problems)


def test_verify_wrong_line_count():
    d = good_draft()
    d["lines"] = d["lines"][:2]
    assert verify_draft(d, SOURCE) == ["Expected exactly 3 lines, got 2."]


def test_verify_ignores_case_whitespace_and_curly_quotes():
    d = good_draft()
    d["lines"][0]["evidence"] = "built  for FREELANCE designers"
    assert verify_draft(d, SOURCE) == []


def test_clean_text_drops_scripts_and_keeps_meta():
    html = """<html><head><title>Acme</title><meta name="description" content="Invoices, fast.">
    <script>var x = 1;</script></head><body><h1>Get paid</h1><p>Get paid</p><p>For designers</p></body></html>"""
    text, _ = _clean_text(html)
    assert "TITLE: Acme" in text and "DESCRIPTION: Invoices, fast." in text
    assert "var x" not in text
    assert text.count("Get paid") == 1  # de-duplicated


def test_build_source_is_capped():
    pages = [Page("https://a.com/", "x" * 50_000), Page("https://a.com/about", "y" * 50_000)]
    assert len(build_source(pages)) < 13_000


def test_generate_retries_once_with_feedback(monkeypatch):
    from opener import core

    monkeypatch.setattr(core, "fetch_site", lambda url: [Page(url, SOURCE * 20)])
    bad = good_draft()
    bad["lines"][0]["evidence"] = "Loved by Fortune 500 teams"
    calls = []

    def fake_draft(source, pitch="", feedback="", model=""):
        calls.append(feedback)
        return (bad if len(calls) == 1 else good_draft()), {"input_tokens": 3000, "output_tokens": 100, "thinking_tokens": 0}

    monkeypatch.setattr(core, "draft_opener", fake_draft)
    r = core.generate("acme.test", use_cache=False)
    assert len(calls) == 2 and "Fortune 500" in calls[1]   # retry told the model what failed
    assert r.attempts == 2 and r.warnings == [] and r.founder == "Jane Doe"
    assert r.usage["input_tokens"] == 6000


def test_verify_catches_filler_and_long_lines():
    d = good_draft()
    d["lines"][1]["text"] = "Scaling invoices brings unique challenges for any team."
    d["lines"][2]["text"] = " ".join(["word"] * 30) + "?"
    problems = verify_draft(d, SOURCE)
    assert any("filler" in p for p in problems) and any("30 words" in p for p in problems)


def test_verify_catches_names_not_on_site():
    d = good_draft()
    d["lines"][2]["text"] = "How do you compete with FreshBooks for designers?"
    assert any('"FreshBooks"' in p for p in verify_draft(d, SOURCE))
    # ...unless the name came from the user's own pitch
    assert verify_draft(d, SOURCE, pitch="we integrate with FreshBooks") == []
