# =============================================================================
# osv1c/report.py — побудова ієрархічної ОСВ із даних PostgreSQL
# =============================================================================
#
# Читає план рахунків (accounts) + залишки по рахунках з проводками (osv_summary)
# і згортає їх по дереву рахунків у вигляд, аналогічний стандартній ОСВ 1С:
#   - ієрархія: 1 → 10 → 104 → 1091 з підсумками на кожному рівні;
#   - 6 колонок: сальдо поч. Дт/Кт, обороти Дт/Кт, сальдо кін. Дт/Кт;
#   - сальдо згортається за видом рахунку (Активний/Пасивний/Активно-пасивний).
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd
import psycopg2

import config


# ---------------------------------------------------------------------------
# Читання з БД
# ---------------------------------------------------------------------------

def _conn():
    return psycopg2.connect(
        host=config.PG_HOST, port=config.PG_PORT, dbname=config.PG_DB,
        user=config.PG_USER, password=config.PG_PASSWORD,
        sslmode=config.PG_SSLMODE,
    )


def available_periods() -> list[str]:
    from osv1c.periods import sort_tags
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT period FROM osv_summary")
        return sort_tags([r[0] for r in cur.fetchall()])


def load_accounts() -> dict[str, dict]:
    """code → {name, parent_code, kind, off_balance}."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT code, name, parent_code, kind, off_balance FROM accounts")
        return {
            r[0]: {"name": r[1], "parent_code": r[2] or "", "kind": r[3] or "",
                   "off_balance": bool(r[4])}
            for r in cur.fetchall()
        }


def load_summary(period: str) -> dict[str, list[float]]:
    """code → [нач.Дт, нач.Кт, об.Дт, об.Кт, кін.Дт, кін.Кт] (розгорнуте, по проводках)."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT account_code,
                      saldo_start_dt, saldo_start_ct,
                      turnover_dt, turnover_ct,
                      saldo_end_dt, saldo_end_ct
               FROM osv_summary WHERE period = %s""",
            (period,),
        )
        return {r[0]: [float(x or 0) for x in r[1:]] for r in cur.fetchall()}


# ---------------------------------------------------------------------------
# Згортання сальдо за видом рахунку
# ---------------------------------------------------------------------------

def _net_saldo(dt_raw: float, ct_raw: float, kind: str,
               expand_active_passive: bool = False) -> tuple[float, float]:
    """
    Згортає сальдо (сума Дт / сума Кт) у вигляд стандартної ОСВ 1С (згорнуте):
      - сальдо = Дт − Кт; додатнє показуємо в Дт, від'ємне — в Кт;
      - для пасивних рахунків бік за замовчуванням — Кт.
    Якщо expand_active_passive=True, активно-пасивні рахунки лишаються
    розгорнутими (обидві сторони) — НЕ як у звичайній ручній ОСВ.
    """
    k = kind.lower()
    active = "актив" in k
    passive = "пасив" in k
    if expand_active_passive and active and passive:
        return dt_raw, ct_raw                       # розгорнуто (опційно)
    if passive and not active:
        net = ct_raw - dt_raw
        return (0.0, net) if net >= 0 else (-net, 0.0)
    # активні, активно-пасивні (згорнуто), невідомі — нетто за знаком у Дт
    net = dt_raw - ct_raw
    return (net, 0.0) if net >= 0 else (0.0, -net)


# ---------------------------------------------------------------------------
# Побудова ієрархії
# ---------------------------------------------------------------------------

_COLS = ["нач_дт", "нач_кт", "об_дт", "об_кт", "кон_дт", "кон_кт"]


def build_osv(period: str,
              accounts: dict[str, dict] | None = None,
              summary: dict[str, list[float]] | None = None,
              only_nonzero: bool = True,
              with_total: bool = True,
              expand_active_passive: bool = False) -> pd.DataFrame:
    """
    Повертає DataFrame ОСВ у порядку дерева рахунків з колонками:
      code, name, kind, parent_code, level, is_total,
      нач_дт, нач_кт, об_дт, об_кт, кон_дт, кон_кт

    Якщо with_total=True, додає підсумковий рядок «Разом» по БАЛАНСОВИХ
    рахунках (забалансові не входять — інакше Дт≠Кт), як у стандартній ОСВ 1С.
    """
    if accounts is None:
        accounts = load_accounts()
    if summary is None:
        summary = load_summary(period)

    # дерево: батько → діти
    children: dict[str, list[str]] = {}
    for code, a in accounts.items():
        children.setdefault(a["parent_code"], []).append(code)
    for lst in children.values():
        lst.sort()

    # розгорнуті суми (raw) по кожному рахунку = власні проводки + усі нащадки
    raw: dict[str, list[float]] = {}

    def rollup(code: str) -> list[float]:
        acc = list(summary.get(code, [0.0] * 6))   # власні проводки (якщо є)
        for ch in children.get(code, []):
            child = rollup(ch)
            for i in range(6):
                acc[i] += child[i]
        raw[code] = acc
        return acc

    roots = sorted(children.get("", []))
    for r in roots:
        rollup(r)

    # формуємо рядки в порядку DFS (preorder), із згорткою сальдо за видом.
    # Гілки, де всі суми нульові, не виводимо (як у стандартній ОСВ).
    rows: list[dict] = []
    nonzero_codes = {c for c, v in raw.items() if any(abs(x) > 0.004 for x in v)}

    def emit(code: str, level: int):
        if only_nonzero and code not in nonzero_codes:
            return
        a = accounts[code]
        r = raw[code]
        ns_dt, ns_ct = _net_saldo(r[0], r[1], a["kind"], expand_active_passive)
        ke_dt, ke_ct = _net_saldo(r[4], r[5], a["kind"], expand_active_passive)
        vals = [ns_dt, ns_ct, r[2], r[3], ke_dt, ke_ct]
        rows.append({
            "code": code, "name": a["name"], "kind": a["kind"],
            "parent_code": a["parent_code"], "level": level, "is_total": False,
            **{c: round(v, 2) for c, v in zip(_COLS, vals)},
        })
        for ch in children.get(code, []):
            emit(ch, level + 1)

    for r in roots:
        emit(r, 0)

    # --- підсумковий рядок «Разом» по балансових рахунках ---
    # Сума згорнутих значень рахунків верхнього рівня (класів), як «Итого» в 1С.
    # Забалансові рахунки не входять (інакше Дт≠Кт).
    if with_total:
        total = [0.0] * 6
        for code in roots:
            a = accounts.get(code, {})
            if a.get("off_balance"):
                continue
            r = raw[code]
            ns_dt, ns_ct = _net_saldo(r[0], r[1], a.get("kind", ""), expand_active_passive)
            ke_dt, ke_ct = _net_saldo(r[4], r[5], a.get("kind", ""), expand_active_passive)
            for i, v in enumerate([ns_dt, ns_ct, r[2], r[3], ke_dt, ke_ct]):
                total[i] += v
        rows.append({
            "code": "__total__", "name": "Разом", "kind": "", "parent_code": "",
            "level": -1, "is_total": True,
            **{c: round(v, 2) for c, v in zip(_COLS, total)},
        })

    cols = ["code", "name", "kind", "parent_code", "level", "is_total", *_COLS]
    df = pd.DataFrame(rows, columns=cols)
    return df


# Зрозумілі підписи колонок для відображення
COLUMN_LABELS = {
    "code": "Рахунок",
    "name": "Найменування",
    "нач_дт": "Сальдо поч. Дт", "нач_кт": "Сальдо поч. Кт",
    "об_дт": "Оборот Дт",      "об_кт": "Оборот Кт",
    "кон_дт": "Сальдо кін. Дт", "кон_кт": "Сальдо кін. Кт",
}


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    period = available_periods()[0]
    df = build_osv(period)
    print(f"Період {period}: {len(df)} рядків ОСВ")
    with pd.option_context("display.max_rows", 40, "display.width", 200):
        show = df.copy()
        show["name"] = show.apply(lambda x: "  " * x["level"] + str(x["name"])[:30], axis=1)
        print(show[["code", "name", *_COLS]].head(40).to_string(index=False))
