"""gta6-trophy-rarity-forecast-01: forecast Exophase GTA VI PS5 trophy earn rates.

A program that reads one case (makefun/gta6-trophies-case/v1) from the input
directory and writes, for each of the case's trophies, five quantiles of that
trophy's earn rate at the case's target, as {"trophies": [{"id": n, "q": [...]}]}.

Model, per trophy (all in logit space, logit(p) = ln(p / (1 - p)) with p the
rate in percent / 100, rates clamped to 0.01..99.99):

* The history is a sequence of reads of Exophase's trophy page. Exophase
  refreshes its counts on its own schedule, so a read shows the rates as of
  its last refresh, and the page can sit unchanged for hours and then jump.
  The launch-night recording starts on a stale page (638 players tracked)
  that jumps to 3,868 at the first refresh. Reads before the first big jump
  in players_tracked (more than 2x between consecutive reads) are therefore
  dropped: they describe a frozen page, not the live process.
* On the remaining reads, fit a weighted least-squares line of logit(rate)
  against ln(1 + hours since the 2026-11-19 00:00 UTC release), using only
  the last 24 hours of reads: trophy growth saturates (rates approach an
  asymptote as the player pool settles), so a line fitted over the whole
  history inherits the steep early slope and overshoots the target, while
  the recent slope is the honest local trend. This is the shape of the
  house curve baseline, but the fitted slope is shrunk by 0.8, because even
  the recent slope overstates a saturating curve's future rise, and the
  spread is the line's prediction error at the target rather than the
  house's wider random walk.
* The median is the shrunk line read at the target. The spread is the
  line's prediction standard error at the target (residual scatter plus
  slope uncertainty) combined with a small random-walk term that grows with
  the horizon, floored and capped so the band stays honest: too narrow pays
  for every trophy outside it, too wide pays at every trophy.
* Flat history (every usable read identical, as in the practice cases, whose
  histories predate the first refresh): there is no trend to fit. The rules
  document that the first refresh lowered every rate but the one at 100%,
  because the tracked-player count jumped while earners grew more slowly,
  so the median is shifted down 0.7 logit units from the last rate and the
  spread is widened to 1.0. A trophy flat at 100% is forecast at 99.99
  throughout: the refresh leaves it at 100%.

Standard library only. No network, no randomness: two runs on the same case
write the same bytes.
"""

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

INPUT_DIR = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
OUTPUT_DIR = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))

LEVELS = [0.05, 0.25, 0.5, 0.75, 0.95]
Z = [-1.645, -0.674, 0.0, 0.674, 1.645]
RELEASE = datetime(2026, 11, 19, 0, 0, tzinfo=timezone.utc)

# Only the most recent reads carry the current trend: older reads sit on the
# steep early part of a saturating curve and would overshoot the target.
FIT_WINDOW_HOURS = 24.0
# The recent slope still overstates a saturating curve's future rise.
SLOPE_SHRINK = 0.8
# A players_tracked jump bigger than this between consecutive reads marks
# the first Exophase refresh; earlier reads are a frozen page.
REFRESH_JUMP = 2.0
# Flat-history branch: pending-refresh dilution and spread (logit units).
FLAT_SHIFT = -0.7
FLAT_SPREAD = 1.0
# Trend branch: random-walk spread per sqrt(day), and spread floor / cap.
RW_SIGMA = 0.15
SPREAD_FLOOR = 0.22
SPREAD_CAP = 1.6
# Keep extrapolated slopes sane: logit units per unit of ln(1 + hours).
SLOPE_CLAMP = 4.0
RESID_FLOOR = 0.08


def clamp_pct(x: float) -> float:
    return min(99.99, max(0.01, x))


def logit(pct: float) -> float:
    p = clamp_pct(pct) / 100.0
    return math.log(p / (1.0 - p))


def sigmoid(l: float) -> float:
    if l >= 0:
        return 100.0 / (1.0 + math.exp(-l))
    e = math.exp(l)
    return 100.0 * e / (1.0 + e)


def round4(x: float):
    """Round to 4 decimals, half up (the starter's rule); whole numbers as int."""
    scaled = x * 10000
    whole = math.floor(scaled)
    if scaled - whole >= 0.5:
        whole += 1
    value = whole / 10000
    return int(value) if value == int(value) else value


def parse_time(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def quantiles_from_logit(l_med: float, spread: float) -> list:
    out = []
    prev = 0.01
    for z in Z:
        q = clamp_pct(sigmoid(l_med + z * spread))
        q = round4(q)
        if q < prev:
            q = prev
        prev = q
        out.append(q)
    return out


def usable_reads(history: list) -> list:
    """Drop reads before the first big players_tracked jump (the first refresh)."""
    start = 0
    prev_n = None
    for i, read in enumerate(history):
        n = read.get("players_tracked")
        if n is None:
            continue
        if prev_n is not None and prev_n > 0 and n > REFRESH_JUMP * prev_n:
            start = i
            break
        prev_n = n
    return history[start:]


def forecast_trophy(points, target_hours: float, horizon_days: float) -> list:
    """points: [(t_hours, rate)] sorted by time, usable reads only."""
    rates = [r for _, r in points if r is not None]
    if not rates:
        # No read ever listed this trophy: neutral prior, wide band.
        return quantiles_from_logit(logit(1.0), 1.2)
    last = rates[-1]
    if last >= 99.99:
        return [99.99, 99.99, 99.99, 99.99, 99.99]

    xs, ls, ws = [], [], []
    t_last = points[-1][0]
    for t, r in points:
        if r is None or t < t_last - FIT_WINDOW_HOURS:
            continue
        xs.append(math.log1p(max(t, 0.0)))
        ls.append(logit(r))
        ws.append(1.0)

    distinct = len(set(ls))
    if len(xs) < 3 or distinct < 2:
        # Flat history: no trend to fit; expect the pending refresh to dilute.
        return quantiles_from_logit(logit(last) + FLAT_SHIFT, FLAT_SPREAD)

    sw = sum(ws)
    xw = sum(w * x for w, x in zip(ws, xs)) / sw
    yw = sum(w * l for w, l in zip(ws, ls)) / sw
    sxx = sum(w * (x - xw) ** 2 for w, x in zip(ws, xs))
    sxy = sum(w * (x - xw) * (l - yw) for w, x, l in zip(ws, xs, ls))
    if sxx < 1e-9:
        return quantiles_from_logit(logit(last) + FLAT_SHIFT, FLAT_SPREAD)

    slope = SLOPE_SHRINK * max(-SLOPE_CLAMP, min(SLOPE_CLAMP, sxy / sxx))
    intercept = yw - slope * xw
    resid = [l - (intercept + slope * x) for x, l in zip(xs, ls)]
    dof = max(1.0, sw - 2.0)
    s_res = max(RESID_FLOOR, math.sqrt(sum(w * r * r for w, r in zip(ws, resid)) / dof))
    n_eff = sw * sw / sum(w * w for w in ws)
    x_t = math.log1p(max(target_hours, 0.0))
    l_hat = intercept + slope * x_t
    l_hat = max(-9.21, min(9.21, l_hat))
    # Prediction error at the target: residual scatter plus slope uncertainty.
    se_pred_sq = s_res * s_res * (1.0 + 1.0 / n_eff + (x_t - xw) ** 2 / sxx)
    spread = math.sqrt(se_pred_sq + (RW_SIGMA * math.sqrt(horizon_days)) ** 2)
    spread = max(SPREAD_FLOOR, min(SPREAD_CAP, spread))
    return quantiles_from_logit(l_hat, spread)


def forecast_case(case: dict, name: str) -> dict:
    if case["levels"] != LEVELS:
        raise ValueError(f"{name} asks for levels {case['levels']}, not {LEVELS}")
    if not case["history"]:
        raise ValueError(f"{name} has no history")
    target = parse_time(case["target"])
    target_hours = (target - RELEASE).total_seconds() / 3600.0
    horizon_days = case["horizon_minutes"] / 1440.0
    reads = usable_reads(case["history"])
    rows = []
    for i, trophy in enumerate(case["trophies"]):
        points = []
        for read in reads:
            rates = read.get("rates") or []
            rate = rates[i] if i < len(rates) else None
            t = (parse_time(read["at"]) - RELEASE).total_seconds() / 3600.0
            points.append((t, rate))
        rows.append({"id": trophy["id"], "q": forecast_trophy(points, target_hours, horizon_days)})
    return {"trophies": rows}


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(INPUT_DIR.glob("*.in")):
        case = json.loads(path.read_text(encoding="utf-8"))
        forecast = forecast_case(case, path.name)
        (OUTPUT_DIR / f"{path.stem}.out").write_text(
            json.dumps(forecast, separators=(",", ":")) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
