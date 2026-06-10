# =============================================================================
# views/load.py — сторінка «Завантаження з 1С»: користувач сам обирає періоди
#                 (квартали або місяці) і викачує їх у PostgreSQL без CLI.
#
# Працює лише на Windows-машині, де встановлена 1С і зареєстрований
# COM-конектор (V83.COMConnector) — тобто там, де лежить база.
# =============================================================================

from __future__ import annotations

import datetime as _dt

import streamlit as st

import config
from osv1c import report as rep
from osv1c.periods import MONTH_NAMES, Period, tag_label

st.title("⚙️ Завантаження з 1С")

# --- що вже є у базі -------------------------------------------------------
try:
    loaded = rep.available_periods()
except Exception:
    loaded = []
if loaded:
    st.caption("У PostgreSQL вже завантажено: "
               + ", ".join(f"**{tag_label(t)}**" for t in loaded))
else:
    st.caption("У PostgreSQL ще немає жодного періоду.")

# --- параметри підключення (за замовчуванням з config.py) -------------------
with st.expander("Підключення до 1С", expanded=False):
    st.caption("Значення за замовчуванням беруться з `config.py`. "
               "Зміни діють у поточній сесії.")
    base_path = st.text_input("Шлях до файлової бази 1С", config.C1_BASE_PATH)
    col_u, col_p = st.columns(2)
    user = col_u.text_input("Користувач", config.C1_USER)
    password = col_p.text_input("Пароль", config.C1_PASSWORD, type="password")
    organization = st.text_input(
        "Організація (підрядок назви; порожньо — всі)",
        config.C1_ORGANIZATION or "")

# --- вибір періодів ----------------------------------------------------------
st.subheader("Періоди для викачки")

kind = st.radio("Тип періоду", ["Квартали", "Місяці"], horizontal=True)
this_year = _dt.date.today().year
years = st.multiselect("Рік(и)", list(range(this_year - 5, this_year + 2)),
                       default=[this_year])

if kind == "Квартали":
    nums = st.multiselect("Квартал(и)", [1, 2, 3, 4], default=[1],
                          format_func=lambda q: f"{q} квартал")
    selected = [Period(y, "Q", q) for y in sorted(years) for q in sorted(nums)]
else:
    nums = st.multiselect("Місяць(і)", list(range(1, 13)), default=[],
                          format_func=lambda m: MONTH_NAMES[m - 1].capitalize())
    selected = [Period(y, "M", m) for y in sorted(years) for m in sorted(nums)]

if selected:
    st.write("Буде викачано: " + ", ".join(f"`{p.tag}` ({p.label})"
                                           for p in selected))
    already = [p.tag for p in selected if p.tag in loaded]
    if already:
        st.info("Періоди " + ", ".join(already) + " вже є в БД — "
                "вони будуть перезаписані свіжими даними з 1С.")
else:
    st.write("Оберіть рік і хоча б один квартал/місяць.")

# --- викачка -----------------------------------------------------------------
if st.button("⬇️ Викачати обрані періоди в БД", type="primary",
             disabled=not selected):
    try:
        from osv1c.connector import Osv1C
    except ImportError:
        st.error(
            "На цій машині немає COM-підтримки (pywin32/Windows). "
            "Викачка з 1С можлива лише там, де встановлена 1С і доступна "
            "файлова база. Опублікований дашборд лише читає PostgreSQL — "
            "дані в нього завантажують локально цією сторінкою або "
            "`python main.py --db`.")
        st.stop()

    from osv1c import db as db_postgres

    with st.status("Викачую з 1С…", expanded=True) as status:
        try:
            if not db_postgres.is_available():
                raise RuntimeError(
                    "PostgreSQL недоступний — перевірте, що сервер запущено "
                    "і налаштування PG_* у config.py правильні.")
            db_postgres.init_schema()

            st.write(f"Підключаюсь до бази: `{base_path}`")
            bot = Osv1C(base_path=base_path, user=user, password=password,
                        organization=organization or None).connect()

            accounts = bot.fetch_accounts()
            db_postgres.upsert_accounts(accounts)
            st.write(f"План рахунків: {len(accounts)} рахунків")

            progress = st.progress(0.0)
            for i, period in enumerate(selected):
                st.write(f"**{period.label}** — зведена ОСВ…")
                summary = bot.fetch_summary(period)
                codes = Osv1C.codes_to_drill(summary)
                st.write(f"  {len(summary)} рахунків, "
                         f"деталізую {len(codes)}…")
                details = []
                for code in codes:
                    details.extend(bot.fetch_detail(code, period))

                db_postgres.upsert_summary(summary)
                by_parent: dict[str, list] = {}
                for r in details:
                    by_parent.setdefault(r.parent_account, []).append(r)
                n_det = 0
                for parent, rows in by_parent.items():
                    n_det += db_postgres.insert_detail(rows, period.tag, parent)
                st.write(f"  ✔ збережено: {len(summary)} рядків ОСВ, "
                         f"{n_det} рядків деталізації")
                progress.progress((i + 1) / len(selected))

            status.update(label="Готово — дані в БД", state="complete")
        except Exception as e:  # noqa: BLE001
            status.update(label="Помилка викачки", state="error")
            st.error(str(e))
            st.stop()

    st.cache_data.clear()   # щоб нові періоди одразу з'явились на інших сторінках
    st.success("Завантажено: " + ", ".join(p.label for p in selected)
               + ". Періоди вже доступні на сторінках «Порівняння» та «ОСВ».")
