"""Chart overlays: trade mark-lines and fair-value-gap mark-areas.

Both were closures inside render(). _build_mark_lines captured nothing;
_build_fvg_areas captured one name, which is now a keyword-only parameter
spelled the same way, so neither body changed by a line.
"""

_BEAR_COL  = "#FF4444"   # bright red   (matches old app)
_BULL_COL  = "#00CC88"   # bright green (matches old app)


def _build_mark_lines(trades: list[dict], tick) -> list[dict]:
    lines = []
    if tick:
        # Bid line (green)
        lines.append({
            "yAxis": tick.bid,
            "lineStyle": {"color": "rgba(0,204,136,0.7)", "width": 1, "type": "dotted"},
            "label": {
                "show": True,
                "formatter": f"Bid {tick.bid:.2f}",
                "color": "#fff",
                "backgroundColor": "rgba(0,204,136,0.75)",
                "padding": [2, 4],
                "fontSize": 10,
                "position": "end",
            },
        })
        # Ask line (blue)
        lines.append({
            "yAxis": tick.ask,
            "lineStyle": {"color": "rgba(100,180,255,0.5)", "width": 1, "type": "dotted"},
            "label": {
                "show": True,
                "formatter": f"Ask {tick.ask:.2f}",
                "color": "#fff",
                "backgroundColor": "rgba(100,180,255,0.5)",
                "padding": [2, 4],
                "fontSize": 10,
                "position": "end",
            },
        })

    # TP1-TP5 and SL lines for every active trade were removed here
    # (2026-08-04, explicit request). With several trades open at once
    # that was up to six dashed lines EACH, which buried the price
    # action and the FVG zones this chart now draws. Each trade's ENTRY
    # line is kept: it is one line per position and it is what locates
    # the trade on the chart at all. The live TP/SL numbers are still
    # on the trades panel beside the chart, so nothing is lost, it is
    # just no longer drawn over the candles.
    for t in trades:
        dir_col  = _BULL_COL if t.get("direction", "").upper() == "BUY" else _BEAR_COL
        entry_p  = float(t["entry_price"])
        lines.append({
            "yAxis": entry_p,
            "lineStyle": {"color": dir_col, "width": 1.5, "type": "solid"},
            "label": {
                "show": True, "formatter": f"Entry {entry_p:.2f}",
                "color": dir_col, "position": "end", "fontSize": 10,
            },
        })
    return lines


def _build_fvg_areas(fvgs: list[dict], *, _bar_index_for_ts) -> list[list[dict]]:
    """ECharts markArea data: each zone is a [start, end] pair.

        Each zone starts at the bar where the gap formed and runs to the
        right edge, which is how a gap actually reads: it is not a level that
        existed before the imbalance that created it. Leaving the x bound off
        entirely (the previous behaviour) drew every zone across the whole
        chart, so a screen of them turned into stacked horizontal bands with
        all the labels piled up on the left.

        Styled to match the reference MT5 chart: a solid block in the gap's
        direction, labelled BUY FVG / SELL FVG. Only two states reach here --
        untested and tested (wicked into but not broken) -- so the tested one
        is drawn a shade lighter rather than given its own colour scheme.
        """
    areas = []
    for f in fvgs:
        bullish = f.get("direction") == "bullish"
        base = "34,197,94" if bullish else "239,68,68"
        # Tested gaps stay drawn but read as the weaker of the two.
        alpha = 0.30 if f.get("filled") else 0.55
        start = {
            "yAxis": f["bottom"],
            "itemStyle": {
                "color": f"rgba({base},{alpha})",
                "borderColor": f"rgba({base},0.9)",
                "borderWidth": 1,
            },
            "label": {
                "show": True,
                "formatter": f"{'BUY' if bullish else 'SELL'} FVG",
                # insideStartTop centres the text on the zone's left edge,
                # which hangs half the label outside the box; align + a
                # small offset tucks it inside.
                "position": "insideStartTop",
                "align": "left",
                "verticalAlign": "top",
                "offset": [5, 3],
                "color": "#e5e7eb",
                "fontSize": 10,
                "fontWeight": "bold",
            },
        }
        bar = _bar_index_for_ts(f["ts"]) if f.get("ts") else None
        if bar is not None:
            # Integer index rather than the axis label: labels repeat
            # across days (bare "13:45" on two dates), and ECharts would
            # resolve the duplicate to the first match.
            start["xAxis"] = bar
        areas.append([start, {"yAxis": f["top"]}])
    return areas
