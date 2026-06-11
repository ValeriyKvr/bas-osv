# =============================================================================
# dashboard.py — точка входу Streamlit-дашборда (навігація між сторінками)
# =============================================================================
#
# Запуск:
#     streamlit run dashboard.py
#
# Сторінки:
#     views/osv.py      — оборотно-сальдова відомість (один період)
#     views/compare.py  — порівняння двох періодів (для презентації)
#
# Потребує заповненої БД (python main.py --db --period 2025-Q1 2026-Q1).
# =============================================================================

import streamlit as st

st.set_page_config(page_title="БАСТІОН-ФУДС — фінансовий аналіз", layout="wide")

pg = st.navigation([
    st.Page("views/compare.py", title="Порівняння періодів", icon="📈",
            default=True),
    st.Page("views/finstate.py", title="Фінансовий стан", icon="🩺"),
    st.Page("views/osv.py", title="ОСВ (відомість)", icon="📊"),
    st.Page("views/load.py", title="Завантаження з 1С", icon="⚙️"),
])
pg.run()
