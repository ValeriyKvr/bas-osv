# =============================================================================
# osv1c/compare.py — порівняння двох періодів (дані з PostgreSQL)
# =============================================================================
#
# Три зрізи для порівняння періодів A та B:
#   compare_osv()          — ієрархічна ОСВ: обороти/сальдо обох періодів поруч;
#   cashflow_items()       — рух грошей (рахунки 30/31/33) за статтями ДДС
#                            (субконто 2 банківських виписок);
#   counterparty_compare() — обороти по контрагентах (субконто 1) для рахунку
#                            розрахунків (631 — постачальники, 361 — покупці).
# =============================================================================

from __future__ import annotations

import pandas as pd
import psycopg2

import config
from osv1c import report as rep

_COLS = rep._COLS  # нач_дт, нач_кт, об_дт, об_кт, кон_дт, кон_кт

# маркери внутрішніх переказів між власними рахунками (стаття ДДС) —
# вони однаково роздувають і надходження, і платежі
INTERNAL_TRANSFER_MARKERS = ("перевед", "перевод", "внутр")

# префікси грошових рахунків (каса, банк, інші кошти)
CASH_PREFIXES = ("30", "31", "33")


def _conn():
    return psycopg2.connect(
        host=config.PG_HOST, port=config.PG_PORT, dbname=config.PG_DB,
        user=config.PG_USER, password=config.PG_PASSWORD,
        sslmode=config.PG_SSLMODE,
    )


# ---------------------------------------------------------------------------
# Ієрархічна ОСВ: два періоди поруч
# ---------------------------------------------------------------------------

def compare_osv(period_a: str, period_b: str) -> pd.DataFrame:
    """
    Повертає DataFrame у порядку дерева рахунків з колонками:
      code, name, kind, parent_code, level,
      <col>_a, <col>_b для кожної з 6 колонок ОСВ,
      кон_нетто_a/b — згорнуте кінцеве сальдо (Дт − Кт).
    Рядки, нульові в ОБОХ періодах, відкидаються.
    """
    accounts = rep.load_accounts()
    a = rep.build_osv(period_a, accounts=accounts,
                      only_nonzero=False, with_total=False)
    b = rep.build_osv(period_b, accounts=accounts,
                      only_nonzero=False, with_total=False)
    # обидва кадри побудовані з одного плану рахунків → однаковий набір рядків
    b = b.set_index("code")

    out = a[["code", "name", "kind", "parent_code", "level"]].copy()
    out["off_balance"] = [accounts.get(c, {}).get("off_balance", False)
                          for c in out["code"]]
    num_cols = []
    for c in _COLS:
        out[c + "_a"] = a[c].values
        out[c + "_b"] = b.loc[out["code"], c].values
        num_cols += [c + "_a", c + "_b"]
    out["кон_нетто_a"] = out["кон_дт_a"] - out["кон_кт_a"]
    out["кон_нетто_b"] = out["кон_дт_b"] - out["кон_кт_b"]

    nonzero = (out[num_cols].abs() > 0.004).any(axis=1)
    return out[nonzero].reset_index(drop=True)


def kpi_row(cmp_df: pd.DataFrame, code: str) -> dict[str, float]:
    """Рядок порівняльної ОСВ за кодом рахунку → dict (нулі, якщо рахунку немає)."""
    r = cmp_df[cmp_df["code"] == code]
    if r.empty:
        return {c: 0.0 for c in cmp_df.columns if c.endswith(("_a", "_b"))}
    return r.iloc[0].to_dict()


def cash_kpis(cmp_df: pd.DataFrame) -> dict[str, float]:
    """Залишок грошей (каса + банк + інші кошти) на початок/кінець обох періодів."""
    out = {"нач_a": 0.0, "нач_b": 0.0, "кон_a": 0.0, "кон_b": 0.0}
    top = cmp_df[(cmp_df["level"] == 1)
                 & cmp_df["code"].str.startswith(CASH_PREFIXES)]
    for _, r in top.iterrows():
        out["нач_a"] += r["нач_дт_a"] - r["нач_кт_a"]
        out["нач_b"] += r["нач_дт_b"] - r["нач_кт_b"]
        out["кон_a"] += r["кон_нетто_a"]
        out["кон_b"] += r["кон_нетто_b"]
    return out


def profit_change(cmp_df: pd.DataFrame) -> tuple[float, float]:
    """
    Фінансовий результат періоду = зміна нетто-сальдо рахунку 44
    (нерозподілений прибуток) за період. Повертає (A, B).
    """
    r = kpi_row(cmp_df, "44")
    if not r:
        return 0.0, 0.0
    res_a = (r["кон_кт_a"] - r["кон_дт_a"]) - (r["нач_кт_a"] - r["нач_дт_a"])
    res_b = (r["кон_кт_b"] - r["кон_дт_b"]) - (r["нач_кт_b"] - r["нач_дт_b"])
    return res_a, res_b


# ---------------------------------------------------------------------------
# Рух грошей за статтями (субконто 2 рахунків 30/31/33)
# ---------------------------------------------------------------------------

def cashflow_items(period_a: str, period_b: str) -> pd.DataFrame:
    """
    Статті руху грошових коштів за два періоди:
      стаття | надходження_a | платежі_a | надходження_b | платежі_b | internal
    internal=True — внутрішній переказ між власними рахунками.
    """
    like = [p + "%" for p in CASH_PREFIXES]
    sql = """
        SELECT period,
               COALESCE(NULLIF(TRIM(subconto2), ''), '(без статті)') AS item,
               SUM(turnover_dt) AS dt, SUM(turnover_ct) AS ct
        FROM osv_detail
        WHERE period IN (%s, %s)
          AND (parent_account LIKE %s OR parent_account LIKE %s
               OR parent_account LIKE %s)
        GROUP BY period, item
    """
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (period_a, period_b, *like))
        rows = cur.fetchall()

    df = pd.DataFrame(rows, columns=["period", "стаття", "dt", "ct"])
    if df.empty:
        return pd.DataFrame(columns=["стаття", "надходження_a", "платежі_a",
                                     "надходження_b", "платежі_b", "internal"])
    df["dt"] = df["dt"].astype(float)
    df["ct"] = df["ct"].astype(float)

    piv = df.pivot_table(index="стаття", columns="period",
                         values=["dt", "ct"], aggfunc="sum", fill_value=0.0)
    out = pd.DataFrame({
        "стаття": piv.index,
        "надходження_a": piv.get(("dt", period_a), 0.0),
        "платежі_a":     piv.get(("ct", period_a), 0.0),
        "надходження_b": piv.get(("dt", period_b), 0.0),
        "платежі_b":     piv.get(("ct", period_b), 0.0),
    }).reset_index(drop=True)
    out["internal"] = out["стаття"].str.lower().apply(
        lambda s: any(m in s for m in INTERNAL_TRANSFER_MARKERS))
    return out.sort_values("платежі_b", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Контрагенти (субконто 1) по рахунку розрахунків
# ---------------------------------------------------------------------------

def counterparty_compare(period_a: str, period_b: str,
                         parent_account: str) -> pd.DataFrame:
    """
    Обороти та кінцеве нетто-сальдо по контрагентах рахунку parent_account:
      контрагент | дт_a | кт_a | нетто_a | дт_b | кт_b | нетто_b
    нетто = сальдо кін. Дт − Кт (для 631 борг постачальнику від'ємний,
    для 361 борг покупця додатний).
    """
    sql = """
        SELECT period,
               COALESCE(NULLIF(TRIM(subconto1), ''), '(не задано)') AS cp,
               SUM(turnover_dt)               AS dt,
               SUM(turnover_ct)               AS ct,
               SUM(saldo_end_dt - saldo_end_ct) AS net
        FROM osv_detail
        WHERE period IN (%s, %s) AND parent_account = %s
        GROUP BY period, cp
    """
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (period_a, period_b, parent_account))
        rows = cur.fetchall()

    df = pd.DataFrame(rows, columns=["period", "контрагент", "dt", "ct", "net"])
    if df.empty:
        return pd.DataFrame(columns=["контрагент", "дт_a", "кт_a", "нетто_a",
                                     "дт_b", "кт_b", "нетто_b"])
    for c in ("dt", "ct", "net"):
        df[c] = df[c].astype(float)

    piv = df.pivot_table(index="контрагент", columns="period",
                         values=["dt", "ct", "net"], aggfunc="sum", fill_value=0.0)
    out = pd.DataFrame({
        "контрагент": piv.index,
        "дт_a":    piv.get(("dt", period_a), 0.0),
        "кт_a":    piv.get(("ct", period_a), 0.0),
        "нетто_a": piv.get(("net", period_a), 0.0),
        "дт_b":    piv.get(("dt", period_b), 0.0),
        "кт_b":    piv.get(("ct", period_b), 0.0),
        "нетто_b": piv.get(("net", period_b), 0.0),
    }).reset_index(drop=True)
    return out


def detail_accounts() -> list[str]:
    """Рахунки, по яких у БД є деталізація (для випадаючих списків)."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT parent_account FROM osv_detail ORDER BY 1")
        return [r[0] for r in cur.fetchall()]
