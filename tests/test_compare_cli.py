"""The `mtgcompare` CLI (`compare.main`): one card or a file of cards,
printing the cheapest offer and optionally exporting every price."""
import json
import sys

from mtgcompare import compare


def _fake_collect(card_name, fx, logger=None):
    if card_name == "Unknown Card":
        return []
    return [
        {"shop": "A", "card": card_name, "price_jpy": 300.0},
        {"shop": "B", "card": card_name, "price_jpy": 100.0},
    ]


def _run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["mtgcompare", *argv])
    monkeypatch.setattr(compare, "get_fx", lambda ccy: 150.0)
    monkeypatch.setattr(compare, "collect_prices", _fake_collect)
    compare.main()


def test_single_card_prints_the_cheapest(monkeypatch, capsys):
    _run(monkeypatch, "-c", "Sol Ring")
    out = capsys.readouterr().out
    assert "For card Sol Ring the cheapest is" in out and '"shop": "B"' in out


def test_file_of_cards_with_export(monkeypatch, capsys, tmp_path):
    cards = tmp_path / "cards.txt"
    cards.write_text("Sol Ring\n\nUnknown Card\n", encoding="utf-8")
    export = tmp_path / "out.json"
    _run(monkeypatch, "-f", str(cards), "-e", str(export))
    out = capsys.readouterr().out
    assert "No prices found for Unknown Card" in out
    rows = json.loads(export.read_text(encoding="utf-8"))
    assert [r["shop"] for r in rows] == ["A", "B"] and all("timestamp" in r for r in rows)
