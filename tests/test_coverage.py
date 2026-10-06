from stockagent import coverage


def test_gaps_listed_for_empty_company(app):
    c = app.watchlist.companies[0]
    gaps = coverage.company_gaps(app, c)
    assert any("没有日线" in g for g in gaps)
    assert any("没有公司画像" in g for g in gaps)
    assert any("没有任何定期报告" in g for g in gaps)
    msgs = coverage.report(app, [c])
    assert msgs[0].startswith("⚠ ") and c.name in msgs[0]
