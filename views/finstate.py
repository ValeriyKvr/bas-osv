# =============================================================================
# views/finstate.py — сторінка «Фінансовий стан»: коефіцієнтний аналіз
#                     з нормативними зонами та інтегральною оцінкою.
# =============================================================================

from __future__ import annotations

import io

import altair as alt
import pandas as pd
import streamlit as st

from osv1c import finstate as fs
from osv1c import report as rep
from osv1c.periods import tag_label


@st.cache_data(ttl=300)
def _periods() -> list[str]:
    return rep.available_periods()


@st.cache_data(ttl=300)
def _compute(periods: tuple[str, ...]):
    return fs.compute(list(periods))


# ---------------------------------------------------------------------------
# Форматування
# ---------------------------------------------------------------------------

def _fmt(v, fmt: str) -> str:
    if v is None or pd.isna(v):
        return "—"
    if fmt == "x":
        return f"{v:,.2f}"
    if fmt == "d":
        return f"{v:,.0f} дн."
    if fmt == "%":
        return f"{v:,.1f} %"
    return f"{v:,.0f}".replace(",", " ")        # uah


_ZONE_CSS = {
    "good": "background-color:#d1e7dd;color:#0a3622;",
    "warn": "background-color:#fff3cd;color:#664d03;",
    "bad":  "background-color:#f8d7da;color:#58151c;font-weight:600;",
}
_GROUP_CSS = "background-color:#1f4e78;color:#ffffff;font-weight:bold;"


st.title("🩺 Фінансовий стан")

all_periods = _periods()
if not all_periods:
    st.error("У базі немає даних. Спочатку завантажте періоди з 1С.")
    st.stop()

with st.sidebar:
    st.header("Періоди")
    sel_periods = st.multiselect("Показати у динаміці", all_periods,
                                 default=all_periods, format_func=tag_label)
if not sel_periods:
    st.info("Оберіть хоча б один період.")
    st.stop()

values, zones, aggs = _compute(tuple(sel_periods))
last = sel_periods[-1]
prev = sel_periods[-2] if len(sel_periods) > 1 else None

st.caption(
    "Усі показники розраховано з даних оборотно-сальдової відомості: "
    "балансові агрегати — за розгорнутими сальдо класів рахунків, "
    "періоди обороту — за середніми залишками та оборотами періоду. "
    "Z″-рахунок Альтмана — орієнтовна інтегральна оцінка."
)

# --- KPI: останній період проти попереднього -------------------------------
_KPI = [("pokr", "Покриття", False), ("avton", "Автономія", False),
        ("dso", "DSO, днів", True), ("zscore", "Z″-рахунок", False)]
cols = st.columns(len(_KPI))
for col, (key, label, inverse) in zip(cols, _KPI):
    r = next(x for x in fs.RATIOS if x.key == key)
    v = values[last][key]
    d = None
    if prev is not None and v is not None and values[prev][key] is not None:
        d = v - values[prev][key]
    col.metric(f"{label} ({tag_label(last)})",
               _fmt(v, r.fmt),
               delta=None if d is None else f"{d:+,.2f}",
               delta_color="inverse" if inverse else "normal")

# --- зведена таблиця по групах ----------------------------------------------
rows, styles = [], []
for group in dict.fromkeys(r.group for r in fs.RATIOS):
    rows.append({"Показник": group, "Норматив": "",
                 **{tag_label(p): "" for p in sel_periods}})
    styles.append({"__group__": True, "zones": {}})
    for r in [x for x in fs.RATIOS if x.group == group]:
        rows.append({
            "Показник": "    " + r.name,
            "Норматив": r.norm,
            **{tag_label(p): _fmt(values[p][r.key], r.fmt) for p in sel_periods},
        })
        styles.append({"__group__": False,
                       "zones": {tag_label(p): zones[p][r.key] for p in sel_periods}})

disp = pd.DataFrame(rows)


def _style_row(row):
    meta = styles[int(row.name)]
    if meta["__group__"]:
        return [_GROUP_CSS] * len(row)
    out = []
    for col_name in disp.columns:
        z = meta["zones"].get(col_name)
        out.append(_ZONE_CSS.get(z, ""))
    return out


st.dataframe(disp.style.apply(_style_row, axis=1),
             width="stretch", hide_index=True,
             height=min(700, 36 * len(disp) + 40))
st.caption("🟩 у нормі  🟨 потребує уваги  🟥 поза нормативом. "
           "Показники без зон — довідкові.")

# --- динаміка обраного показника --------------------------------------------
if len(sel_periods) > 1:
    st.subheader("📈 Динаміка показника")
    name_by_key = {r.key: r.name for r in fs.RATIOS}
    key = st.selectbox("Показник", [r.key for r in fs.RATIOS],
                       format_func=lambda k: name_by_key[k])
    r = next(x for x in fs.RATIOS if x.key == key)
    chart_df = pd.DataFrame({
        "Період": [tag_label(p) for p in sel_periods],
        "Значення": [values[p][key] for p in sel_periods],
        "__order": range(len(sel_periods)),
    }).dropna(subset=["Значення"])
    if chart_df.empty:
        st.info("Немає даних для побудови графіка.")
    else:
        chart = (alt.Chart(chart_df).mark_bar(size=46, color="#2e6da4")
                 .encode(x=alt.X("Період:N", sort=alt.SortField("__order"), title=None),
                         y=alt.Y("Значення:Q", title=r.name),
                         tooltip=["Період", alt.Tooltip("Значення:Q", format=",.2f")])
                 .properties(height=300))
        st.altair_chart(chart, width="stretch")

# --- балансові агрегати (довідково) ------------------------------------------
with st.expander("Балансові агрегати (довідково), грн"):
    agg_rows = [
        ("Необоротні активи", "необоротні_кін"),
        ("Запаси", "запаси_кін"),
        ("Дебіторська заборгованість (36, 37)", "дебіторська_кін"),
        ("Грошові кошти (30, 31, 33)", "гроші_кін"),
        ("Оборотні активи, разом", "оборотні_кін"),
        ("АКТИВИ (валюта балансу)", "активи_кін"),
        ("Власний капітал", "власний_кін"),
        ("Довгострокові зобов'язання", "довгострокові_кін"),
        ("Поточні зобов'язання", "поточні_кін"),
        ("у т.ч. кредиторська постачальникам (63)", "кредиторська_кін"),
        ("Виручка за період (оборот Кт 70)", "виручка"),
        ("Собівартість за період (оборот Дт 90)", "собівартість"),
        ("Фінрезультат періоду (зміна рах. 44)", "фінрезультат"),
    ]
    agg_disp = pd.DataFrame({
        "Стаття": [n for n, _ in agg_rows],
        **{tag_label(p): [f"{aggs[p][k]:,.0f}".replace(",", " ")
                          for _, k in agg_rows] for p in sel_periods},
    })
    st.dataframe(agg_disp, width="stretch", hide_index=True,
                 height=36 * len(agg_disp) + 40)
    st.caption("Сальдо на кінець періоду. Розрахункові класи (3, 6) — "
               "розгорнуто: дебетові залишки в активі, кредитові в пасиві.")

# --- експорт -----------------------------------------------------------------
@st.cache_data(ttl=300)
def _excel(periods: tuple[str, ...]) -> bytes:
    values, zones, aggs = fs.compute(list(periods))
    coef = pd.DataFrame([
        {"Група": r.group, "Показник": r.name, "Норматив": r.norm,
         **{tag_label(p): values[p][r.key] for p in periods}}
        for r in fs.RATIOS
    ])
    agg = pd.DataFrame([
        {"Агрегат": k, **{tag_label(p): aggs[p].get(k) for p in periods}}
        for k in aggs[periods[0]].keys() if k != "days"
    ])
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        coef.to_excel(xw, index=False, sheet_name="Коефіцієнти")
        agg.to_excel(xw, index=False, sheet_name="Агрегати")
    return buf.getvalue()


st.download_button(
    "⬇️ Завантажити аналіз в Excel",
    data=_excel(tuple(sel_periods)),
    file_name=f"фінансовий_стан_{'_'.join(sel_periods)}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)
