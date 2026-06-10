# =============================================================================
# views/osv.py — сторінка «ОСВ»: відомість з PostgreSQL у вигляді, аналогічному
#                1q.xlsx, з розгортанням рівнів ієрархії (клік по рядку).
#                Запускається через dashboard.py (st.navigation).
# =============================================================================

from __future__ import annotations

import io

import pandas as pd
import psycopg2
import streamlit as st

import config
from osv1c import report as rep
from osv1c.periods import tag_label

_NUM_COLS = rep._COLS  # нач_дт, нач_кт, об_дт, об_кт, кон_дт, кон_кт
_LABEL = {
    "нач_дт": "Сальдо поч. Дт", "нач_кт": "Сальдо поч. Кт",
    "об_дт": "Оборот Дт", "об_кт": "Оборот Кт",
    "кон_дт": "Сальдо кін. Дт", "кон_кт": "Сальдо кін. Кт",
}


# ---------------------------------------------------------------------------
# Кешоване читання з БД
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300)
def _periods() -> list[str]:
    return rep.available_periods()


@st.cache_data(ttl=300)
def _osv(period: str, only_nonzero: bool) -> pd.DataFrame:
    return rep.build_osv(period, only_nonzero=only_nonzero)


@st.cache_data(ttl=300)
def _detail(period: str, parent_account: str) -> pd.DataFrame:
    cols = ["Рахунок", "Субконто 1", "Субконто 2", "Субконто 3",
            "Сальдо поч. Дт", "Сальдо поч. Кт", "Оборот Дт", "Оборот Кт",
            "Сальдо кін. Дт", "Сальдо кін. Кт"]
    sql = """SELECT account_code, subconto1, subconto2, subconto3,
                    saldo_start_dt, saldo_start_ct, turnover_dt, turnover_ct,
                    saldo_end_dt, saldo_end_ct
             FROM osv_detail
             WHERE period = %s AND parent_account = %s
             ORDER BY subconto1, subconto2, subconto3"""
    with psycopg2.connect(
        host=config.PG_HOST, port=config.PG_PORT, dbname=config.PG_DB,
        user=config.PG_USER, password=config.PG_PASSWORD,
        sslmode=config.PG_SSLMODE,
    ) as conn, conn.cursor() as cur:
        cur.execute(sql, (period, parent_account))
        rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=cols)
    # числові колонки з NUMERIC приходять як Decimal — приводимо до float
    for c in cols[4:]:
        df[c] = df[c].astype(float)
    return df


# ---------------------------------------------------------------------------
# Дерево: видимі рядки залежно від розгорнутих вузлів
# ---------------------------------------------------------------------------

def _visible_rows(full: pd.DataFrame, expanded: set[str], parents: set[str]) -> pd.DataFrame:
    """Лишає рядки, всі предки яких розгорнуті. Корені (level 0) видимі завжди."""
    parent_of = dict(zip(full["code"], full["parent_code"]))
    visible_codes: set[str] = set()
    for code in full["code"]:
        p = parent_of.get(code, "")
        ok = True
        # піднімаємось по предках: усі мають бути розгорнуті
        while p:
            if p not in expanded:
                ok = False
                break
            p = parent_of.get(p, "")
        if ok:
            visible_codes.add(code)
    return full[full["code"].isin(visible_codes)].copy()


_INDENT_UNIT = "        "  # 8 нерозривних пробілів на рівень


def _name_with_marker(row, expanded: set[str], parents: set[str]) -> str:
    if row.get("is_total"):
        return "РАЗОМ (баланс)"
    indent = _INDENT_UNIT * int(row["level"])
    if row["code"] in parents:
        marker = "▾ " if row["code"] in expanded else "▸ "
    else:
        marker = "   "
    return f"{indent}{marker}{row['name']}"


def _display_frame(df: pd.DataFrame, expanded: set[str], parents: set[str]) -> pd.DataFrame:
    out = pd.DataFrame()
    out["Рахунок"] = [("" if t else c) for c, t in zip(df["code"], df.get("is_total", False))]
    out["Найменування"] = [
        _name_with_marker(r, expanded, parents) for _, r in df.iterrows()
    ]
    for c in _NUM_COLS:
        col = df[c].astype(float).reset_index(drop=True)
        out[_LABEL[c]] = col.where(col.abs() > 0.004, other=pd.NA)
    return out


def _num_config(cols) -> dict:
    return {c: st.column_config.NumberColumn(format="localized") for c in cols}


# Кольори «сходинок» за рівнем рахунку (білий текст на синіх відтінках —
# гарно і в темній, і в світлій темі): (фон, текст, жирність)
_LEVEL_STYLE = {
    0: ("#15324f", "#ffffff", "bold"),    # клас (1-ша ступінь) — найтемніший
    1: ("#1f4e78", "#ffffff", "bold"),    # 2-га ступінь
    2: ("#2e6da4", "#ffffff", "bold"),    # 3-тя ступінь
    3: ("#4a89c0", "#ffffff", "normal"),  # 4-та ступінь
}
_DEEP_STYLE = None                                 # глибше — без заливки (тема за замовч.)
_TOTAL_STYLE = ("#0f5132", "#ffffff", "bold")      # підсумок «Разом» — зелений


def _styler(disp: pd.DataFrame, levels: list[int], totals: list[bool]):
    """Ступінчасте підсвічування рядків за рівнем + формат чисел."""
    def style_row(row):
        i = int(row.name)
        style = _TOTAL_STYLE if totals[i] else _LEVEL_STYLE.get(levels[i], _DEEP_STYLE)
        if style is None:
            return [""] * len(row)
        bg, col, fw = style
        return [f"background-color:{bg};color:{col};font-weight:{fw}"] * len(row)

    num_cols = [c for c in disp.columns if c not in ("Рахунок", "Найменування")]
    fmt = {c: (lambda v: "" if pd.isna(v) else f"{v:,.2f}") for c in num_cols}
    return disp.style.apply(style_row, axis=1).format(fmt)


# ---------------------------------------------------------------------------
# Деталізація по рахунку — ступінчастий вигляд (Субконто1 → 2 → 3 з підсумками)
# ---------------------------------------------------------------------------

def _detail_stepped(det: pd.DataFrame):
    """
    Будує ієрархію деталізації: групує по активних субконто з підсумками на
    кожному рівні. Повертає (disp, levels, leaf_flags) для рендера.
    """
    num = [_LABEL[c] for c in _NUM_COLS]
    sub_cols = ["Субконто 1", "Субконто 2", "Субконто 3"]

    def _nonempty(col: pd.Series) -> bool:
        return col.fillna("").astype(str).str.strip().ne("").any()

    hier = []
    if det["Рахунок"].nunique() > 1:
        hier.append("Рахунок")
    hier += [c for c in sub_cols if _nonempty(det[c])]
    if not hier:
        hier = ["Рахунок"]

    out: list[dict] = []

    def recurse(df: pd.DataFrame, lvl: int):
        col = hier[lvl]
        last = lvl == len(hier) - 1
        for val, g in df.groupby(col, sort=False, dropna=False):
            name = str(val).strip() if val is not None else ""
            if name in ("", "nan", "None"):
                name = "(не задано)"
            row = {"level": lvl, "name": name, "leaf": last}
            for c in num:
                row[c] = float(g[c].sum())
            out.append(row)
            if not last:
                recurse(g, lvl + 1)

    recurse(det, 0)

    disp = pd.DataFrame()
    disp["Аналітика"] = [_INDENT_UNIT * r["level"] + r["name"] for r in out]
    for c in num:
        s = pd.Series([r[c] for r in out], dtype=float)
        disp[c] = s.where(s.abs() > 0.004, other=pd.NA)
    levels = [r["level"] for r in out]
    leaf = [r["leaf"] for r in out]
    return disp, levels, leaf


def _styler_detail(disp: pd.DataFrame, levels: list[int], leaf: list[bool]):
    """Підсвічує рядки-підсумки за рівнем; листові рядки — без заливки."""
    def style_row(row):
        i = int(row.name)
        if leaf[i]:
            return [""] * len(row)
        bg, col, fw = _LEVEL_STYLE.get(levels[i], _LEVEL_STYLE[3])
        return [f"background-color:{bg};color:{col};font-weight:{fw}"] * len(row)

    num_cols = [c for c in disp.columns if c != "Аналітика"]
    fmt = {c: (lambda v: "" if pd.isna(v) else f"{v:,.2f}") for c in num_cols}
    return disp.style.apply(style_row, axis=1).format(fmt)


def _total_bar(t) -> None:
    """Окремий зафіксований рядок «Разом» під таблицею (не скролиться разом з нею)."""
    row = {"Рахунок": "", "Найменування": "РАЗОМ (баланс)"}
    for c in _NUM_COLS:
        row[_LABEL[c]] = float(t[c])
    disp = pd.DataFrame([row])
    bg, col, _ = _TOTAL_STYLE
    sty = (disp.style
           .apply(lambda r: [f"background-color:{bg};color:{col};font-weight:bold"] * len(r), axis=1)
           .format({_LABEL[c]: (lambda v: f"{v:,.2f}") for c in _NUM_COLS}))
    st.dataframe(sty, width="stretch", hide_index=True)


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

st.title("📊 Оборотно-сальдова відомість")

periods = _periods()
if not periods:
    st.error("У базі немає даних ОСВ. Спочатку виконайте: `python main.py --db`")
    st.stop()

with st.sidebar:
    st.header("Параметри")
    period = st.selectbox("Період", periods, index=len(periods) - 1,
                          format_func=tag_label)
    only_nonzero = st.checkbox("Лише ненульові", value=True)
    search = st.text_input("Пошук (код або назва)", "").strip().lower()
    st.divider()
    st.caption("Натисніть на рядок-групу (▸), щоб розгорнути/згорнути рівень.")

full = _osv(period, only_nonzero)
tree = full[~full["is_total"]]                     # рахунки без підсумкового рядка
total_row = full[full["is_total"]]
parents = set(tree["parent_code"].dropna()) - {""}  # коди, що мають дочірні рахунки
all_parent_codes = parents & set(tree["code"])

# стан розгортання тримаємо в session_state (окремо для кожного періоду)
state_key = f"expanded::{period}::{only_nonzero}"
if state_key not in st.session_state:
    # за замовчуванням розгорнуті лише корені (показуємо рівні 0 та 1)
    st.session_state[state_key] = set(tree[tree["level"] == 0]["code"])
expanded: set[str] = st.session_state[state_key]

# --- кнопки керування ---
b1, b2, _ = st.columns([1, 1, 6])
if b1.button("➕ Розгорнути все"):
    st.session_state[state_key] = set(all_parent_codes)
    st.rerun()
if b2.button("➖ Згорнути все"):
    st.session_state[state_key] = set()
    st.rerun()

# --- KPI (з підсумкового рядка «Разом» по балансу) ---
c1, c2, c3 = st.columns(3)
c1.metric("Рахунків у звіті", len(tree))
if not total_row.empty:
    t = total_row.iloc[0]
    c2.metric("Оборот за період", f"{t['об_дт']:,.2f}")
    c3.metric("Валюта балансу (кін.)", f"{t['кон_дт']:,.2f}")

st.caption(f"Період: **{period}**. Сальдо згорнуте (нетто за знаком), "
           f"по організації з config.C1_ORGANIZATION — як у ручній ОСВ 1С.")

# --- режим пошуку: плаский список без дерева ---
if search:
    hit = tree[tree["code"].str.lower().str.contains(search)
               | tree["name"].str.lower().str.contains(search)].reset_index(drop=True)
    disp = _display_frame(hit, set(), parents)  # без маркерів розгортання
    st.dataframe(_styler(disp, hit["level"].tolist(), hit["is_total"].tolist()),
                 width="stretch", hide_index=True, height=560)
    st.info("Режим пошуку: показано всі збіги пласким списком. "
            "Очистіть пошук, щоб повернутись до дерева.")
else:
    visible = _visible_rows(full, expanded, parents)
    visible = visible[~visible["is_total"]].reset_index(drop=True)   # «Разом» — окремо знизу
    disp = _display_frame(visible, expanded, parents)

    ev = st.dataframe(
        _styler(disp, visible["level"].tolist(), visible["is_total"].tolist()),
        width="stretch", hide_index=True, height=560,
        on_select="rerun", selection_mode="single-row", key="osv_sel",
    )

    # --- обробка кліку: розгортання/згортання ---
    rows = ev.selection.rows if ev and ev.selection else []
    token = tuple(rows)
    if token != st.session_state.get("osv_tok"):
        st.session_state["osv_tok"] = token
        if rows:
            picked = visible.iloc[rows[0]]
            code = str(picked["code"])
            if code in parents:
                # рахунок-група → розгортаємо/згортаємо рівень
                if code in expanded:
                    expanded.discard(code)
                else:
                    expanded.add(code)
                st.session_state["last_parent"] = code if code in expanded else None
                st.rerun()
            elif not bool(picked["is_total"]) and code:
                # кінцевий рахунок → відкриваємо його деталізацію нижче
                st.session_state["drill_account"] = code
                st.session_state["last_parent"] = None
                st.rerun()
        else:
            # порожнє виділення (повторний клік по виділеному рядку) — згортаємо його
            lp = st.session_state.get("last_parent")
            if lp and lp in expanded:
                expanded.discard(lp)
                st.session_state["last_parent"] = None
                st.rerun()

# --- зафіксований рядок «Разом» одразу під таблицею (не треба гортати грід) ---
if not total_row.empty:
    _total_bar(total_row.iloc[0])

# --- Експорт повної ОСВ в Excel ---
def _to_excel(df: pd.DataFrame) -> bytes:
    exp = pd.DataFrame({
        "Рахунок": [("" if t else c) for c, t in zip(df["code"], df["is_total"])],
        "Найменування": ["    " * max(l, 0) + str(n) for l, n in zip(df["level"], df["name"])],
    })
    for c in _NUM_COLS:
        exp[_LABEL[c]] = df[c]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        exp.to_excel(xw, index=False, sheet_name=f"ОСВ {period}")
    return buf.getvalue()

st.download_button(
    "⬇️ Завантажити повну ОСВ в Excel",
    data=_to_excel(full),
    file_name=f"osv_{period}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)

# --- Деталізація по рахунку (субконто) ---
st.subheader("🔍 Деталізація по рахунку (субконто)")
st.caption("💡 Клацніть рядок кінцевого рахунку в таблиці вище — його деталізація "
           "відкриється тут. Або оберіть/введіть код нижче.")
codes = tree["code"].tolist()
name_by_code = dict(zip(tree["code"], tree["name"]))
# синхронізація з кліком по таблиці: значення тримаємо в session_state["drill_account"]
if codes and st.session_state.get("drill_account") not in codes:
    st.session_state["drill_account"] = codes[0]
sel = st.selectbox("Рахунок (пошук по коду або назві)", codes, key="drill_account",
                   format_func=lambda c: f"{c} — {name_by_code.get(c, '')}")
if sel:
    det = _detail(period, sel)
    if det.empty:
        st.info(f"По рахунку {sel} немає деталізації по субконто.")
    else:
        stepped = st.toggle("Ступінчастий вигляд (по субконто з підсумками)", value=True)
        st.caption(f"Рахунок {sel}: {len(det)} рядків аналітики")
        if stepped:
            d_disp, d_levels, d_leaf = _detail_stepped(det)
            st.dataframe(_styler_detail(d_disp, d_levels, d_leaf),
                         width="stretch", hide_index=True, height=400)
        else:
            st.dataframe(
                det, width="stretch", hide_index=True, height=400,
                column_config=_num_config([c for c in det.columns
                                           if c not in ("Рахунок", "Субконто 1", "Субконто 2", "Субконто 3")]),
            )
