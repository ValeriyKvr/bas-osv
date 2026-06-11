# =============================================================================
# osv1c/finstate.py — фінансові коефіцієнти та інтегральна оцінка стану
# =============================================================================
#
# Усі показники рахуються з даних ОСВ (PostgreSQL):
#   - балансові агрегати — з нетто-сальдо класів рахунків на кінець періоду;
#   - показники активності — з оборотів за період та середніх залишків;
#   - інтегральна оцінка — спрощена модель Альтмана Z'' для непублічних
#     компаній (орієнтовна, на агрегатах ОСВ).
#
# Зони: 'good' / 'warn' / 'bad' / None (без нормативу).
# =============================================================================

from __future__ import annotations

import calendar
from dataclasses import dataclass
from typing import Callable, Optional

from osv1c import report as rep
from osv1c.periods import parse as parse_period


# ---------------------------------------------------------------------------
# Балансові агрегати з ОСВ
# ---------------------------------------------------------------------------

def _days(period_tag: str) -> int:
    p = parse_period(period_tag)
    return sum(calendar.monthrange(p.year, m)[1]
               for m in range(p.start_month, p.end_month + 1))


def aggregates(period: str, summary: dict | None = None) -> dict[str, float]:
    """
    Агрегати за період: балансові групи (на початок/кінець) та обороти.

    Працює з СИРИМИ сальдо рахунків з проводками (osv_summary), де сальдо
    розгорнуте по субконто. Розрахункові класи (3, 6) беремо розгорнуто:
    дебетові залишки — в актив (дебіторська, переплати), кредитові —
    в пасив (аванси отримані, кредиторська). Класи 1, 2, 4 — нетто
    (контрактивні рахунки на кшталт зносу 13 згортаються коректно).
    """
    if summary is None:
        summary = rep.load_summary(period)
    # індекси у векторі: 0 нач_дт, 1 нач_кт, 2 об_дт, 3 об_кт, 4 кін_дт, 5 кін_кт

    def s(prefixes, idx: int) -> float:
        if isinstance(prefixes, str):
            prefixes = (prefixes,)
        return sum(v[idx] for code, v in summary.items()
                   if code.startswith(tuple(prefixes)) and code[:1].isdigit())

    out: dict[str, float] = {"days": _days(period)}
    for tag, dt, ct in (("поч", 0, 1), ("кін", 4, 5)):
        necur = s("1", dt) - s("1", ct)                       # необоротні (нетто зносу)
        stocks = s("2", dt) - s("2", ct)                      # запаси
        cash = s(("30", "31", "33"), dt) - s(("30", "31", "33"), ct)
        receiv = s(("36", "37"), dt)
        cur_assets = stocks + s("3", dt) + s("6", dt)
        equity = s("4", ct) - s("4", dt)                      # власний капітал (Кт)
        lt_liab = s("5", ct) - s("5", dt)
        st_liab = s("6", ct) + s("3", ct)                     # кл.6 Кт + аванси отримані
        out.update({
            f"необоротні_{tag}": necur,
            f"запаси_{tag}": stocks,
            f"гроші_{tag}": cash,
            f"дебіторська_{tag}": receiv,
            f"оборотні_{tag}": cur_assets,
            f"активи_{tag}": necur + cur_assets,
            f"власний_{tag}": equity,
            f"довгострокові_{tag}": lt_liab,
            f"поточні_{tag}": st_liab,
            f"кредиторська_{tag}": s("63", ct),
            f"нерозп_прибуток_{tag}": s("44", ct) - s("44", dt),
        })

    out["виручка"] = s("70", 3)                # дохід (оборот Кт 70)
    out["собівартість"] = s("90", 2)
    out["закупівлі"] = s("63", 3)              # нараховано постачальниками
    out["фінрезультат"] = out["нерозп_прибуток_кін"] - out["нерозп_прибуток_поч"]
    return out


# ---------------------------------------------------------------------------
# Коефіцієнти
# ---------------------------------------------------------------------------

def _avg(a: dict, key: str) -> float:
    return (a[f"{key}_поч"] + a[f"{key}_кін"]) / 2.0


def _div(x: float, y: float) -> Optional[float]:
    return None if abs(y) < 0.5 else x / y


@dataclass(frozen=True)
class Ratio:
    key: str
    name: str
    group: str
    norm: str                                   # текст нормативу для таблиці
    fmt: str                                    # 'x' — коеф., 'd' — дні, '%' , 'uah'
    fn: Callable[[dict], Optional[float]]
    zone: Callable[[float], Optional[str]]      # 'good'/'warn'/'bad'/None


def _z_altman(a: dict) -> Optional[float]:
    """Спрощений Z''-рахунок Альтмана для непублічних компаній."""
    assets = a["активи_кін"]
    liab = a["довгострокові_кін"] + a["поточні_кін"]
    if abs(assets) < 0.5 or abs(liab) < 0.5:
        return None
    x1 = (a["оборотні_кін"] - a["поточні_кін"]) / assets
    x2 = a["нерозп_прибуток_кін"] / assets
    x3 = a["фінрезультат"] / assets
    x4 = a["власний_кін"] / liab
    return 6.56 * x1 + 3.26 * x2 + 6.72 * x3 + 1.05 * x4


RATIOS: list[Ratio] = [
    # --- ліквідність ---
    Ratio("pokr", "Коефіцієнт покриття (поточна ліквідність)", "Ліквідність",
          "≥ 1,5", "x",
          lambda a: _div(a["оборотні_кін"], a["поточні_кін"]),
          lambda v: "good" if v >= 1.5 else ("warn" if v >= 1.0 else "bad")),
    Ratio("shvyd", "Коефіцієнт швидкої ліквідності", "Ліквідність",
          "≥ 0,6", "x",
          lambda a: _div(a["оборотні_кін"] - a["запаси_кін"], a["поточні_кін"]),
          lambda v: "good" if v >= 0.6 else ("warn" if v >= 0.3 else "bad")),
    Ratio("absol", "Коефіцієнт абсолютної ліквідності", "Ліквідність",
          "≥ 0,2", "x",
          lambda a: _div(a["гроші_кін"], a["поточні_кін"]),
          lambda v: "good" if v >= 0.2 else ("warn" if v >= 0.1 else "bad")),
    # --- стійкість ---
    Ratio("avton", "Коефіцієнт автономії (власний капітал / активи)",
          "Фінансова стійкість", "≥ 0,5", "x",
          lambda a: _div(a["власний_кін"], a["активи_кін"]),
          lambda v: "good" if v >= 0.5 else ("warn" if v >= 0.3 else "bad")),
    Ratio("zalezh", "Коефіцієнт фінансової залежності (зобов'язання / активи)",
          "Фінансова стійкість", "≤ 0,5", "x",
          lambda a: _div(a["довгострокові_кін"] + a["поточні_кін"], a["активи_кін"]),
          lambda v: "good" if v <= 0.5 else ("warn" if v <= 0.7 else "bad")),
    Ratio("robkap", "Робочий капітал (оборотні − поточні зобов'язання), грн",
          "Фінансова стійкість", "> 0", "uah",
          lambda a: a["оборотні_кін"] - a["поточні_кін"],
          lambda v: "good" if v > 0 else "bad"),
    # --- ділова активність ---
    Ratio("dso", "Період погашення дебіторської заборгованості (DSO), днів",
          "Ділова активність", "≤ 60", "d",
          lambda a: _div(_avg(a, "дебіторська") * a["days"], a["виручка"]),
          lambda v: "good" if v <= 60 else ("warn" if v <= 120 else "bad")),
    Ratio("dpo", "Період погашення кредиторської заборгованості (DPO), днів",
          "Ділова активність", "довідково", "d",
          lambda a: _div(_avg(a, "кредиторська") * a["days"], a["закупівлі"]),
          lambda v: None),
    Ratio("dio", "Період обороту запасів (DIO), днів",
          "Ділова активність", "довідково", "d",
          lambda a: _div(_avg(a, "запаси") * a["days"], a["собівартість"]),
          lambda v: None),
    Ratio("fincycle", "Фінансовий цикл (DSO + DIO − DPO), днів",
          "Ділова активність", "менше — краще", "d",
          lambda a: (None if None in (
              _div(_avg(a, "дебіторська") * a["days"], a["виручка"]),
              _div(_avg(a, "запаси") * a["days"], a["собівартість"]),
              _div(_avg(a, "кредиторська") * a["days"], a["закупівлі"]))
              else _div(_avg(a, "дебіторська") * a["days"], a["виручка"])
              + _div(_avg(a, "запаси") * a["days"], a["собівартість"])
              - _div(_avg(a, "кредиторська") * a["days"], a["закупівлі"])),
          lambda v: None),
    # --- рентабельність ---
    Ratio("rent", "Рентабельність продажів за фінрезультатом періоду, %",
          "Рентабельність", "> 0", "%",
          lambda a: _div(a["фінрезультат"] * 100.0, a["виручка"]),
          lambda v: "good" if v > 0 else "bad"),
    # --- інтегральна оцінка ---
    Ratio("zscore", "Z''-рахунок Альтмана (інтегральна оцінка, орієнтовно)",
          "Інтегральна оцінка", "> 2,6", "x",
          _z_altman,
          lambda v: "good" if v > 2.6 else ("warn" if v >= 1.1 else "bad")),
]


def compute(periods: list[str]) -> tuple[dict, dict, dict]:
    """
    Рахує все одним проходом:
      values[period][key], zones[period][key], aggs[period] (агрегати).
    """
    values: dict[str, dict] = {}
    zones: dict[str, dict] = {}
    aggs: dict[str, dict] = {}
    for p in periods:
        a = aggregates(p)
        aggs[p] = a
        values[p] = {}
        zones[p] = {}
        for r in RATIOS:
            try:
                v = r.fn(a)
            except Exception:
                v = None
            values[p][r.key] = v
            zones[p][r.key] = r.zone(v) if v is not None else None
    return values, zones, aggs
