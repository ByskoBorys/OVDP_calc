import io
import streamlit as st
import pandas as pd
from datetime import date
import sys, platform, os

import streamlit as st
st.set_page_config(page_title="ОВДП калькулятор", layout="wide")

st.write("✅ Boot checkpoint A: Streamlit imported")
st.caption(f"Python {sys.version.split()[0]} on {platform.platform()}")
st.caption(f"Working dir: {os.getcwd()}")

try:
    import pandas as pd
    from data_loader import load_df
    from bond_utils import (
        secondary_price_from_yield,
        primary_price_from_yield_minfin,
        yields_from_price,
        build_cashflow_schedule,
        trade_outcome
    )
    st.write("✅ Boot checkpoint B: modules imported")
except Exception as e:
    st.error(f"❌ Import error: {e}")
    st.stop()


import streamlit as st
import pandas as pd
from datetime import date

from data_loader import load_df
from bond_utils import (
    secondary_price_from_yield,
    primary_price_from_yield_minfin,
    yields_from_price,
    build_cashflow_schedule,
    trade_outcome
)

st.set_page_config(page_title="ОВДП калькулятор", layout="wide")

def _to_xlsx_bytes(sheets: dict) -> bytes:
    """
    sheets: {"SheetName": pd.DataFrame або dict/список пар}
    Повертає байти XLSX для st.download_button.
    """
    bio = io.BytesIO()
    with pd.ExcelWriter(bio, engine="openpyxl") as writer:
        for name, data in sheets.items():
            if isinstance(data, pd.DataFrame):
                data.to_excel(writer, sheet_name=name[:31], index=False)
            elif isinstance(data, dict):
                pd.DataFrame(list(data.items()), columns=["Поле","Значення"]).to_excel(writer, sheet_name=name[:31], index=False)
            else:
                pd.DataFrame([data]).to_excel(writer, sheet_name=name[:31], index=False)
    bio.seek(0)
    return bio.getvalue()


st.title("📈 Калькулятор ОВДП")

# ------- Загрузка данных -------
with st.spinner("Завантажуємо дані НБУ..."):
    try:
        df, asof = load_df()
    except Exception as e:
        st.error(f"Не вдалось завантажити дані НБУ: {e}")
        st.stop()

st.sidebar.success(f"Дані НБУ станом на: {asof}")
st.sidebar.caption("Джерело: bank.gov.ua/files/Fair_value/sec_hdbk.xls")

with st.sidebar.expander("FAQ / Що вміє калькулятор?"):
    st.markdown(
        """
**Калькулятор** переводить **ціну ↔ дохідність** для ОВДП.

- **Вторинний ринок (ціна):**  
  • Для **дисконтних** та **купонних з останнім купоном** ціна рахується за **формулою СІМ**.  
  • Для **усіх інших купонних** — через **YTM** (ефективна дохідність).

- **Первинний ринок (ціна Мінфіну):**  
  • **Дисконтні** — за **формулою СІМ**.  
  • **Купонні** — за **формулою Мінфіну** (середньозважена дохідність розміщення).

- **Угода Купив → Продав:**  
  Розрахунок P&L з урахуванням **dirty-цін** на дати купівлі/продажу, **отриманих купонів**, кількості днів володіння та **річної простої дохідності**.
        """
    )


# ------- Табы -------
tab_calc, tab_trade = st.tabs(["Калькулятор", "Розрахувати угоду"])

# ======================== КАЛЬКУЛЯТОР ========================
with tab_calc:
    st.subheader("Розрахунок ціни ↔ дохідності")
    c1, c2, c3, c4 = st.columns([1.5, 1.2, 1.2, 1.2])

    with c1:
        isin = st.selectbox("ISIN", sorted(df["ISIN"].dropna().unique()))
        calc_date = st.date_input("Дата розрахунку", value=date.today())

    with c2:
        market = st.radio("Ринок", ["Вторинний", "Первинний (Мінфін)"], horizontal=True)
        input_mode = st.radio("Ввід", ["Дохідність (%)", "Ціна (dirty)"], horizontal=True)

    with c3:
        if input_mode == "Дохідність (%)":
            y_val = st.number_input("Дохідність, %", value=10.00, step=0.01, format="%.2f")
        else:
            p_val = st.number_input("Ціна (dirty)", value=1000.00, step=0.01, format="%.2f")

    st.divider()

    if st.button("Розрахувати", type="primary"):
        try:
            if input_mode == "Дохідність (%)":
                if market == "Вторинний":
                    dirty, ai, clean, ccy, formula = secondary_price_from_yield(str(calc_date), isin, y_val, df)
                else:
                    dirty, ai, clean, ccy, formula = primary_price_from_yield_minfin(str(calc_date), isin, y_val, df)

                sched, coupon_rate, _ccy = build_cashflow_schedule(df, isin, from_date=str(calc_date))

                st.success(
                    f"**Валюта:** {ccy} • **Купон (номінальна ставка):** {round(coupon_rate*100,2)}%"
                )
                st.info(
                    f"**Результат:** Dirty: **{dirty} {ccy}** | НКД: **{ai}** | Clean: **{clean}** | Формула: **{formula}**"
                )
                st.markdown("**Графік купонів та погашення (від дати розрахунку):**")
                st.dataframe(sched, use_container_width=True)

            else:
                res = yields_from_price(str(calc_date), isin, p_val, df)
                st.success(
                    f"**Валюта:** {res['Currency']} • "
                    f"**Вторинний ринок:** {res['Secondary_yield']}% ({res['Secondary_formula']}) • "
                    f"**Первинний (Мінфін):** {res['Primary_yield']}% ({res['Primary_formula']})"
                )

                sched, coupon_rate, ccy = build_cashflow_schedule(df, isin, from_date=str(calc_date))
                st.markdown(
                    f"**Купон (номінальна ставка):** {round(coupon_rate*100,2)}% • **Валюта:** {ccy}"
                )
                st.dataframe(sched, use_container_width=True)

        except Exception as e:
            st.error(f"Помилка розрахунку: {e}")

# ======================== УГОДА (купив → продав) ========================
with tab_trade:
    st.subheader("P&L угоди (купив → продав)")
    c1, c2 = st.columns(2)

    with c1:
        isin_t = st.selectbox("ISIN для угоди", sorted(df["ISIN"].dropna().unique()))
        buy_date = st.date_input("Дата покупки", value=date.today())
        buy_y = st.number_input("Дохідність покупки, % (вторинний ринок)", value=10.00, step=0.01, format="%.2f")

    with c2:
        sell_date = st.date_input("Дата продажу", value=date.today())
        sell_y = st.number_input("Дохідність продажу, % (вторинний ринок)", value=9.50, step=0.01, format="%.2f")

    if st.button("Порахувати P&L", type="primary"):
        try:
            res = trade_outcome(isin_t, str(buy_date), buy_y, str(sell_date), sell_y, df)

            st.success(f"**Валюта:** {res['Currency']} • **Тримали днів:** {res['Days_held']}")
            st.write(
                f"**Ціна покупки (dirty):** {res['Buy']['price_dirty']} | "
                f"**Ціна продажу (dirty):** {res['Sell']['price_dirty']}"
            )
            st.write(
                f"**Отримані купони:** {res['Coupons_total']} | "
                f"**P&L, сума:** {res['Profit_abs']} | **Річна проста %:** {res['Profit_ann_pct']}%"
            )

            if res.get("Coupons_received"):
                st.markdown("**Купони за період володіння:**")
                st.dataframe(pd.DataFrame(res["Coupons_received"], columns=["Дата", "Сума"]), use_container_width=True)
            else:
                st.info("За період володіння купонів не було.")
        except Exception as e:
            st.error(f"Помилка розрахунку P&L: {e}")
