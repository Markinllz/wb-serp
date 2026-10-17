from pathlib import Path

from wb_serp.queries import read_queries


def test_pdf_source_preserves_70_phrases():
    source = Path(__file__).resolve().parents[1] / "config" / "queries.txt"
    queries = read_queries(source)
    assert len(queries) == 70
    assert queries[0] == "постельное белье 2 спальное"
    assert "постельное бельё" in queries
    assert "пастельный комплект 1,5" in queries
    assert queries[-1] == "постельное белье 1,5"
