# =============================================================================
# osv1c/db.py — PostgreSQL: создание схемы и загрузка данных
# =============================================================================

from __future__ import annotations
import psycopg2
import psycopg2.extras
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from osv1c.connector import SummaryRow, DetailRow, AccountDim

import config


def _conn():
    return psycopg2.connect(
        host=config.PG_HOST,
        port=config.PG_PORT,
        dbname=config.PG_DB,
        user=config.PG_USER,
        password=config.PG_PASSWORD,
        sslmode=config.PG_SSLMODE,
    )


# ---------------------------------------------------------------------------
# Схема
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS accounts (
    code         VARCHAR(20) PRIMARY KEY,   -- код счёта "104"
    name         TEXT,
    parent_code  VARCHAR(20),               -- код родителя "10" ("" если корень)
    kind         TEXT,                      -- "Активний"/"Пасивний"/"Активний/Пасивний"
    off_balance  BOOLEAN DEFAULT FALSE      -- забалансовый счёт
);
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS off_balance BOOLEAN DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS osv_summary (
    id               SERIAL PRIMARY KEY,
    period           VARCHAR(20)  NOT NULL,   -- "2025-Q1"
    account_code     VARCHAR(20)  NOT NULL,
    account_name     TEXT,
    level            SMALLINT,                -- 1=раздел, 2=счёт, 3=субсчёт, 4=аналитика
    saldo_start_dt   NUMERIC(18,2),
    saldo_start_ct   NUMERIC(18,2),
    turnover_dt      NUMERIC(18,2),
    turnover_ct      NUMERIC(18,2),
    saldo_end_dt     NUMERIC(18,2),
    saldo_end_ct     NUMERIC(18,2),
    loaded_at        TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (period, account_code)
);

CREATE TABLE IF NOT EXISTS osv_detail (
    id               SERIAL PRIMARY KEY,
    period           VARCHAR(20)  NOT NULL,
    parent_account   VARCHAR(20)  NOT NULL,   -- счёт детализации "631"
    account_code     VARCHAR(20),             -- счёт из виртуальной таблицы
    subconto1        TEXT,                    -- 1-е субконто (контрагент и т.д.)
    subconto2        TEXT,                    -- 2-е субконто (договор и т.д.)
    subconto3        TEXT,                    -- 3-е субконто
    saldo_start_dt   NUMERIC(18,2),
    saldo_start_ct   NUMERIC(18,2),
    turnover_dt      NUMERIC(18,2),
    turnover_ct      NUMERIC(18,2),
    saldo_end_dt     NUMERIC(18,2),
    saldo_end_ct     NUMERIC(18,2),
    loaded_at        TIMESTAMPTZ DEFAULT NOW()
);

-- Удобные индексы
CREATE INDEX IF NOT EXISTS idx_summary_period  ON osv_summary(period);
CREATE INDEX IF NOT EXISTS idx_summary_code    ON osv_summary(account_code);
CREATE INDEX IF NOT EXISTS idx_detail_period   ON osv_detail(period);
CREATE INDEX IF NOT EXISTS idx_detail_parent   ON osv_detail(parent_account);
"""


def init_schema():
    """Создаёт таблицы если не существуют."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
        conn.commit()
    print("[DB] Схема готова.")


# ---------------------------------------------------------------------------
# Загрузка данных
# ---------------------------------------------------------------------------

def upsert_accounts(rows: "list[AccountDim]") -> int:
    """Завантажує план рахунків (ієрархія). При конфлікті коду — оновлює."""
    if not rows:
        return 0
    sql = """
        INSERT INTO accounts (code, name, parent_code, kind, off_balance)
        VALUES %s
        ON CONFLICT (code) DO UPDATE SET
            name        = EXCLUDED.name,
            parent_code = EXCLUDED.parent_code,
            kind        = EXCLUDED.kind,
            off_balance = EXCLUDED.off_balance
    """
    values = [(r.code, r.name, r.parent_code, r.kind, r.off_balance) for r in rows]
    with _conn() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(cur, sql, values)
        conn.commit()
    return len(values)


def upsert_summary(rows: "list[SummaryRow]") -> int:
    """Загружает сводную ОСВ. При конфликте периода+кода — обновляет."""
    if not rows:
        return 0

    sql = """
        INSERT INTO osv_summary
            (period, account_code, account_name, level,
             saldo_start_dt, saldo_start_ct,
             turnover_dt, turnover_ct,
             saldo_end_dt, saldo_end_ct)
        VALUES %s
        ON CONFLICT (period, account_code) DO UPDATE SET
            account_name   = EXCLUDED.account_name,
            level          = EXCLUDED.level,
            saldo_start_dt = EXCLUDED.saldo_start_dt,
            saldo_start_ct = EXCLUDED.saldo_start_ct,
            turnover_dt    = EXCLUDED.turnover_dt,
            turnover_ct    = EXCLUDED.turnover_ct,
            saldo_end_dt   = EXCLUDED.saldo_end_dt,
            saldo_end_ct   = EXCLUDED.saldo_end_ct,
            loaded_at      = NOW()
    """

    values = [
        (r.period, r.account_code, r.account_name, r.level,
         r.saldo_start_dt, r.saldo_start_ct,
         r.turnover_dt, r.turnover_ct,
         r.saldo_end_dt, r.saldo_end_ct)
        for r in rows
    ]

    periods = sorted({r.period for r in rows})
    with _conn() as conn:
        with conn.cursor() as cur:
            # чистимо період(и) перед вставкою — інакше лишаються застарілі
            # рахунки від попередніх завантажень (напр. зі зміною організації)
            cur.execute("DELETE FROM osv_summary WHERE period = ANY(%s)", (periods,))
            psycopg2.extras.execute_values(cur, sql, values)
        conn.commit()

    return len(values)


def insert_detail(rows: "list[DetailRow]", period: str, parent_account: str) -> int:
    """
    Загружает детализацию по счёту.
    Перед вставкой удаляет старые данные за тот же период+счёт.
    """
    if not rows:
        return 0

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM osv_detail WHERE period = %s AND parent_account = %s",
                (period, parent_account),
            )

            sql = """
                INSERT INTO osv_detail
                    (period, parent_account, account_code,
                     subconto1, subconto2, subconto3,
                     saldo_start_dt, saldo_start_ct,
                     turnover_dt, turnover_ct,
                     saldo_end_dt, saldo_end_ct)
                VALUES %s
            """
            values = [
                (r.period, r.parent_account, r.account_code,
                 r.subconto1, r.subconto2, r.subconto3,
                 r.saldo_start_dt, r.saldo_start_ct,
                 r.turnover_dt, r.turnover_ct,
                 r.saldo_end_dt, r.saldo_end_ct)
                for r in rows
            ]
            psycopg2.extras.execute_values(cur, sql, values)
        conn.commit()

    return len(values)


def is_available() -> bool:
    """Чи доступний PostgreSQL (для м'якого пропуску, якщо БД не піднято)."""
    try:
        conn = _conn()
        conn.close()
        return True
    except Exception:
        return False
