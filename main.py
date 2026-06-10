# =============================================================================
# main.py — точка входу: ОСВ з 1С (через COM) → Excel (+ опційно PostgreSQL)
# =============================================================================
#
# Використання:
#   python main.py                 # викачати ОСВ з 1С → Excel у exports/,
#                                   #   і завантажити в PG, якщо БД доступна
#   python main.py --no-db         # тільки Excel, без PostgreSQL
#   python main.py --db            # вимагати PostgreSQL (помилка, якщо не доступний)
#   python main.py --init-db       # тільки створити таблиці в PG
#
# Періоди задаються у config.PERIODS — список (рік, квартал).
# =============================================================================

import sys
import argparse

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

import config


def cmd_init_db():
    from osv1c.db import init_schema
    init_schema()


def cmd_full_cycle(db_mode: str = "auto", periods=None):
    """
    Повний цикл: 1С (COM) → Excel → (опційно) PostgreSQL.
    db_mode: "auto" — вантажити в PG якщо доступний; "off" — ніколи;
             "require" — обов'язково (помилка, якщо PG недоступний).
    periods: список Period / тегів / (рік, квартал);
             якщо None — береться з config.PERIODS.
    """
    from osv1c.periods import parse as parse_period
    periods = [parse_period(p) for p in (periods or config.PERIODS)]
    from osv1c.connector import Osv1C
    from osv1c.excel import write_period_workbook
    from osv1c import db as db_postgres

    # --- визначаємо, чи працюємо з PostgreSQL ---
    use_db = False
    if db_mode == "require":
        use_db = True
    elif db_mode == "auto":
        use_db = db_postgres.is_available()
        if not use_db:
            print("[i] PostgreSQL недоступний — зберігаю лише в Excel. "
                  "(використайте --db, щоб вимагати БД)")

    if use_db:
        db_postgres.init_schema()

    # --- підключення до 1С ---
    bot = Osv1C().connect()

    # --- план рахунків (ієрархія) — потрібен для побудови ОСВ ---
    accounts = bot.fetch_accounts()
    print(f"  план рахунків: {len(accounts)} рахунків")
    if use_db:
        n_acc = db_postgres.upsert_accounts(accounts)
        print(f"  [PG] accounts: {n_acc} рядків")

    for period in periods:
        tag = period.tag
        print(f"\n[=== {tag} ({period.label}) ===]")

        # 1. Зведена ОСВ
        summary = bot.fetch_summary(period)
        print(f"  зведена ОСВ: {len(summary)} рахунків")

        # 2. Деталізація по кожному рахунку
        codes = Osv1C.codes_to_drill(summary)
        print(f"  деталізую {len(codes)} рахунків...")
        all_details = []
        for code in codes:
            rows = bot.fetch_detail(code, period)
            all_details.extend(rows)
        print(f"  деталі: {len(all_details)} рядків (розріз субконто)")

        # 3. PostgreSQL (опційно) — вантажимо ПЕРШИМ, бо це головне джерело
        if use_db:
            n_sum = db_postgres.upsert_summary(summary)
            print(f"  [PG] osv_summary: {n_sum} рядків")
            # деталі вантажимо порціями по батьківському рахунку
            by_parent: dict[str, list] = {}
            for r in all_details:
                by_parent.setdefault(r.parent_account, []).append(r)
            n_det = 0
            for parent, rows in by_parent.items():
                n_det += db_postgres.insert_detail(rows, tag, parent)
            print(f"  [PG] osv_detail: {n_det} рядків")

        # 4. Excel (best-effort: не валимо процес, якщо файл відкритий)
        try:
            out = write_period_workbook(tag, summary, all_details)
            print(f"  [Excel] {out}")
        except PermissionError:
            print(f"  [!] Excel osv_{tag}.xlsx відкритий/заблокований — пропускаю запис.")

    print("\n[✓] Готово!")


# ---------------------------------------------------------------------------

def _parse_periods(values):
    """Парсить періоди: '2026-Q1' (квартал) або '2026-M03' (місяць)."""
    from osv1c.periods import parse as parse_period
    out = []
    for v in values:
        try:
            out.append(parse_period(v))
        except ValueError as e:
            raise SystemExit(str(e))
    return out


def main():
    parser = argparse.ArgumentParser(description="1С ОСВ → Excel/PostgreSQL (через COM)")
    parser.add_argument("--init-db", action="store_true", help="Тільки створити таблиці в PG")
    parser.add_argument("--no-db",   action="store_true", help="Тільки Excel, без PostgreSQL")
    parser.add_argument("--db",      action="store_true", help="Вимагати PostgreSQL")
    parser.add_argument("--period",  nargs="+", metavar="PERIOD",
                        help="Період(и) для викачки: квартал '2026-Q1' або "
                             "місяць '2026-M03' (можна кілька). "
                             "Якщо не вказано — береться з config.PERIODS")
    args = parser.parse_args()

    periods = _parse_periods(args.period) if args.period else None

    if args.init_db:
        cmd_init_db()
    elif args.no_db:
        cmd_full_cycle(db_mode="off", periods=periods)
    elif args.db:
        cmd_full_cycle(db_mode="require", periods=periods)
    else:
        cmd_full_cycle(db_mode="auto", periods=periods)


if __name__ == "__main__":
    main()
