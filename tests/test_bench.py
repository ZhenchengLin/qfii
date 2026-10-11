from datetime import date

from qfii.bench import bench_debate, format_table, save_report


async def test_bench_reports_metrics(cfg, tmp_path):
    report = await bench_debate(cfg, "NVDA", date(2026, 10, 9))
    assert report["outcome"] == "invest" and report["ballots"] == 9
    assert report["first_try_valid"] == 9 and report["size_violations"] == 0
    assert report["models"]["neutral"] == "fake-strongest"
    assert len(report["transcript"]) == 3
    path = save_report(report, tmp_path, "fake")
    assert path.exists()
    table = format_table([report])
    assert "first-try valid" in table and "9/9" in table
