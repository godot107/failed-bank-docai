"""Loan Desk Q&A: quote checks, fixed-query arguments, tool specs and knowledge chunking (no Azure)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from agent import DATA_SPEC, QUERIES, query_args, verify_quotes  # noqa: E402
from knowledge import chunk, doc_label, handbook_passages  # noqa: E402

EVIDENCE = ['{"results": [{"title": "Servicer comment log \\u00b7 loan 231370616", "page": 1, '
            '"text": "05/02/2010 Borrower called: PAYOFF STMT SENT TO TITLE CO. Closing scheduled next month."}]}']


def test_exact_quote_is_verified_despite_case_and_curly_quotes():
    a = "The log says “payoff stmt sent to title co.” (Servicer comment log, p.1)."
    assert verify_quotes(a, EVIDENCE) == [{"quote": "payoff stmt sent to title co.", "verified": True}]


def test_paraphrase_inside_quotes_is_caught():
    a = 'The servicer wrote "a payoff statement was mailed to the title company".'
    assert verify_quotes(a, EVIDENCE)[0]["verified"] is False


def test_short_quotes_are_ignored():
    assert verify_quotes('Status is "pending review" today.', EVIDENCE) == []


def test_query_args_bound_and_clean_values():
    assert query_args("largest_failures", {"top_n": 500, "year": "2010", "state": "ga "}) == [25, 2010, 2010, "GA", "GA"]
    assert query_args("loan_summary", {"loan_number": "231-370-616; DROP TABLE x"}) == ["231370616"]
    assert query_args("find_loans", {})[:2] == ["", ""]                       # every filter optional


def test_missing_required_argument_and_unknown_query():
    with pytest.raises(ValueError):
        query_args("loan_details", {})
    with pytest.raises(ValueError):
        query_args("drop_everything", {})


def test_every_query_argument_is_in_the_tool_spec():
    props = DATA_SPEC["function"]["parameters"]["properties"]
    for name, (_, sql, params) in QUERIES.items():
        assert all(p in props for p in params), name
        if sql is None:                                                       # computed in Python (release_plan)
            continue
        assert sql.count("?") == len(params), name                           # placeholders match arguments
        assert all(p in props for p in params), name
        assert not any(w in sql.upper() for w in ("INSERT ", "UPDATE ", "DELETE ", "EXEC")), name   # read-only


def test_chunk_packs_paragraphs_and_splits_long_ones():
    parts = chunk(["a " * 10, "b " * 10, "x. " * 700], max_chars=200)
    assert all(len(p) <= 200 for p in parts)
    assert parts[0].startswith("a a") and "b b" in parts[0]


def test_handbook_passages_keep_section_and_drop_headers():
    layout = {"paragraphs": [
        {"role": "pageHeader", "content": "FDIC Resolutions Handbook", "boundingRegions": [{"pageNumber": 22}]},
        {"role": "sectionHeading", "content": "Shared Loss P&As", "boundingRegions": [{"pageNumber": 22}]},
        {"content": "A P&A with Shared Loss is similar to a Whole Bank P&A transaction, except that the FDIC shares losses.",
         "boundingRegions": [{"pageNumber": 22}]},
        {"role": "pageNumber", "content": "22", "boundingRegions": [{"pageNumber": 22}]}]}
    rows = handbook_passages(layout)
    assert len(rows) == 1
    assert rows[0][5] == 22 and rows[0][6].endswith("Shared Loss P&As") and rows[0][8].startswith("A P&A with Shared")


def test_document_labels():
    assert doc_label("servicer_comments") == "Servicer comment log"
    assert doc_label("some_new_type") == "Some new type"


def test_words_between_two_quotes_are_not_a_quote():
    a = 'The field is "open" and the check "warn" until a reviewer settles the monthly payment value.'
    assert verify_quotes(a, EVIDENCE) == []          # "open" / "warn" are too short; the gap between them isn't a quote


def test_escaped_unicode_in_tool_output_matches():
    ev = ['{"rows": [{"pool": "Commercial & industrial \\u00b7 non-performing (90+ days)"}]}']
    assert verify_quotes('Pool: "Commercial & industrial · non-performing (90+ days)".', ev)[0]["verified"]


def test_elided_quote_needs_every_piece_in_order():
    ok = 'It says "Borrower called: PAYOFF STMT SENT ... Closing scheduled next month".'
    swapped = 'It says "Closing scheduled next month ... Borrower called: PAYOFF STMT".'
    assert verify_quotes(ok, EVIDENCE)[0]["verified"] is True
    assert verify_quotes(swapped, EVIDENCE)[0]["verified"] is False


def test_figures_trace_to_data_with_unit_conversion_and_rounding():
    from agent import verify_figures
    ev = ['{"rows": [{"fail_year": 2009, "failures": 140, "assets_k": 169712345, "current_upb": 380589.55}]}']
    a = ("In 2009, 140 banks failed with about $169.7 billion in assets. Loan 231370616 owes $380,589.55 "
         "(p.1); one bank had $55.2 billion.")
    got = {f["figure"]: f["verified"] for f in verify_figures(a, ev)}
    assert got == {"$169.7 billion": True, "$380,589.55": True, "$55.2 billion": False}


def test_percentages_match_fractions_or_printed_values():
    from agent import verify_figures
    ev = ['{"rows": [{"sf_loss_share": "80%", "ratio": 0.66}]}']
    got = {f["figure"]: f["verified"] for f in verify_figures("Loss share was 80% and LTV 66%; another 95%.", ev)}
    assert got == {"80%": True, "66%": True, "95%": False}


def test_noise_filter_drops_references_and_axes_not_prose():
    from knowledge import is_noise
    assert is_noise('"Causes and Consequences of Recent Bank Failures." GAO-13-71. 2013. https://www.gao.gov/a.pdf. '
                    'House report. 2012. https://x.gov/b.pdf')
    assert is_noise("Figure 6.2. Total Assets of Failed Banks by Year 400 350 $341.2 300 250 200 $195.7 150 100")
    assert not is_noise("A loan sale is one of the disposition strategies that the FDIC uses to sell loans retained "
                        "from one or more failed banks.")


def test_failure_rows_keep_decimal_costs(monkeypatch):
    import knowledge
    monkeypatch.setattr(knowledge, "failures", lambda: [
        {"ID": "1", "CERT": 19040, "NAME": "THE BANK OF MIAMI", "CITY": "CORAL GABLES", "PSTALP": "FL",
         "FAILDATE": "12/17/2010", "FAILYR": "2010", "RESTYPE": "FAILURE", "RESTYPE1": "PA", "BIDNAME": "1ST UNITED",
         "QBFASSET": 448150, "QBFDEP": 400000, "COST": 28965.429},
        {"ID": "2", "CERT": 1, "NAME": "X", "FAILDATE": "1/1/2008", "FAILYR": "2008", "RESTYPE": "ASSISTANCE"}])
    rows = knowledge.failure_rows()
    assert len(rows) == 1 and rows[0][-1] == 28965        # cost kept (rounded $ thousands); assistance dropped
