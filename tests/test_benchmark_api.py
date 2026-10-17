"""The synthetic workload must be isolated from the live data path."""
from scripts.benchmark_sqlite import bench


def test_disposable_benchmark_checks_counts_and_null_history():
    result = bench(hours=2, queries=3, top=4)
    assert result['runs'] == 6
    assert result['observations'] == 24
    assert result['unique_catalog_rows'] == 24
    assert result['sqlite_integrity'] == 'ok'
    assert 'synthetic' in result['source']
