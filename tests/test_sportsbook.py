from dataclasses import replace
from datetime import timedelta

import pytest
from conftest import NOW, book, make_event, make_market

from pmscan.models import Team
from pmscan.odds import BookEvent, devig, parse_odds_events, same_team
from pmscan.strategies import SportsbookValue
from pmscan.strategies.sportsbook import match_event, moneyline_bets

KICKOFF = NOW + timedelta(hours=5)


def odds_event(prices_by_book: dict[str, dict[str, float]], home="Houston Texans", away="Indianapolis Colts"):
    return {
        "id": "abc", "sport_key": "americanfootball_nfl", "commence_time": KICKOFF.isoformat().replace("+00:00", "Z"),
        "home_team": home, "away_team": away,
        "bookmakers": [{"key": k, "title": k, "markets": [
            {"key": "h2h", "outcomes": [{"name": n, "price": p} for n, p in prices.items()]},
            {"key": "h2h_lay", "outcomes": [{"name": n, "price": 99} for n in prices]},
        ]} for k, prices in prices_by_book.items()],
    }


class FakeOdds:
    def __init__(self, events):
        self.events = events
        self.requests_remaining = "480"
        self.calls = []

    def odds(self, sport_key, active_sports=1):
        self.calls.append(sport_key)
        return self.events

    def ttl_for(self, active_sports, now=None):
        return 3600.0


def test_devig_removes_margin():
    p = devig({"A": 1.91, "B": 1.91})
    assert p == pytest.approx({"A": 0.5, "B": 0.5})
    assert devig({"A": 1.0}) is None


def test_consensus_is_median_of_books_and_ignores_lay_markets():
    [ev] = parse_odds_events([odds_event({
        "b1": {"Houston Texans": 1.5, "Indianapolis Colts": 2.8},
        "b2": {"Houston Texans": 1.6, "Indianapolis Colts": 2.5},
        "b3": {"Houston Texans": 1.55, "Indianapolis Colts": 2.6},
    })])
    fair = ev.fair(min_books=3)
    assert sum(fair.values()) == pytest.approx(1.0)
    assert 0.6 < fair["Houston Texans"] < 0.67
    assert ev.fair(min_books=4) is None
    assert set(ev.fair(bookmakers=["b1"]).keys()) == {"Houston Texans", "Indianapolis Colts"}


@pytest.mark.parametrize("a,b,expected", [
    ("Texans", "Houston Texans", True),
    ("Paris FC", "Paris FC", True),
    ("HB Køge", "HB Koge", True),
    ("Manchester United", "Manchester City", False),
    ("Lakers", "Los Angeles Clippers", False),
])
def test_same_team(a, b, expected):
    assert same_team(a, b) is expected


def nfl_event(ask=0.55, bid=0.53):
    m = replace(make_market(1, bid=bid, ask=ask, group=None), outcomes=("Texans", "Colts"),
                sports_market_type="moneyline", question="Colts vs. Texans")
    ev = make_event([m])
    ev.sport, ev.start_time = "nfl", KICKOFF
    ev.teams = (Team("Indianapolis Colts", "ind"), Team("Houston Texans", "hou", home=True))
    return ev


def test_match_event_requires_teams_and_kickoff():
    be = BookEvent("x", "nfl", KICKOFF + timedelta(minutes=30), "Houston Texans", "Indianapolis Colts")
    assert match_event(nfl_event(), [be], 90) is be
    late = replace(be, commence=KICKOFF + timedelta(hours=3))
    assert match_event(nfl_event(), [late], 90) is None
    other = replace(be, home="Dallas Cowboys")
    assert match_event(nfl_event(), [other], 90) is None


def test_moneyline_bets_us_and_soccer_shapes():
    fair = {"Houston Texans": 0.6, "Indianapolis Colts": 0.4}
    bets = list(moneyline_bets(nfl_event(), fair, 3))
    assert [(b.side, b.fair, b.label) for b in bets] == [(0, 0.6, "Texans to win"), (1, 0.4, "Colts to win")]

    soccer_fair = {"Paris FC": 0.5, "HB Køge": 0.2, "Draw": 0.3}
    win = replace(make_market(1, bid=0.4, ask=0.42, group="g"), group_title="Paris FC", sports_market_type="moneyline")
    draw = replace(make_market(2, bid=0.3, ask=0.31, group="g"), group_title="Draw (Paris FC vs. HB Køge)",
                   sports_market_type="moneyline")
    bets = {(b.market.id, b.side): b for b in moneyline_bets(make_event([win, draw]), soccer_fair, 3)}
    assert bets[("1", 0)].fair == 0.5 and bets[("1", 1)].fair == 0.5
    assert bets[("2", 0)].fair == 0.3 and bets[("2", 1)].label == "No draw"


def test_strategy_finds_value_and_sizes_it(ctx):
    raw = odds_event({b: {"Houston Texans": 1.55, "Indianapolis Colts": 2.6} for b in ("b1", "b2", "b3")})
    odds = FakeOdds(parse_odds_events([raw]))
    strat = SportsbookValue(odds)
    ev = nfl_event(ask=0.55, bid=0.53)  # Texans at 55c vs ~62.6% fair
    books = {"y1": book("y1", [(0.55, 100), (0.58, 100), (0.70, 100)])}
    opps = [o for c in strat.screen([ev], ctx) if (o := strat.evaluate(c, books, ctx))]
    assert odds.calls == ["americanfootball_nfl"]
    [opp] = opps
    assert opp.subtitle == "Texans to win"
    assert opp.legs[0].shares == pytest.approx(200)  # 0.70 level is worse than fair / (1 + min_edge)
    assert opp.details["ev"] > 0.03
    assert opp.value_per_share == pytest.approx(opp.details["fair"])
    assert ctx.stats["sportsbook_matched"] == 1


def test_strategy_inactive_without_key(ctx, monkeypatch):
    monkeypatch.delenv("PMSCAN_ODDS_API_KEY", raising=False)
    assert SportsbookValue().screen([nfl_event()], ctx) == []
    assert ctx.stats["sportsbook_status"].startswith("inactive")


def test_odds_refresh_slows_down_to_last_the_month():
    from datetime import datetime, timezone

    from pmscan.odds import OddsApiClient

    c = OddsApiClient("k", refresh_minutes=60)
    assert c.ttl_for(3) == 3600.0  # unknown budget: the configured refresh
    c.requests_remaining = "100"
    mid = datetime(2026, 11, 16, tzinfo=timezone.utc)  # 15 days left
    # 3 sports x one refresh per ttl must fit in 90 usable credits over 15 days
    assert c.ttl_for(3, mid) == pytest.approx(15 * 86400 * 3 / 90)
    c.requests_remaining = "5"
    assert c.ttl_for(3, mid) == pytest.approx(15 * 86400)  # out of credits: wait for the reset
