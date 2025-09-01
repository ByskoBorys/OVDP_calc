import streamlit as st
import pandas as pd
from data_loader import load_df
from bond_utils import (
    secondary_price_from_yield, primary_price_from_yield_minfin,
    yields_from_price, build_cashflow_schedule, trade_outcome
)

st.set_page_config(page_title="ОВДП калькулятор", layout="wide")
st.title("📈 Калькулятор ОВДП")

df, asof = load_df()
st.sidebar.success(f"Дані НБУ станом на: {asof}")
st.sidebar.caption("Джерело: bank.gov.ua/files/Fair_value/sec_hdbk.xls")

tab_calc, tab_trade = st.tabs(["Калькулятор", "Розрахувати угоду"])

# ================== TAB 1: КАЛЬКУЛЯТОР ==================
with tab_calc:
    st.subheader("Єдине вікно: або дохідність → ціна, або ціна (dirty) → дохідність")

    col1, col2, col3 = st.columns([2,2,2])

    with col1:
        isin = st.selectbox("Оберіть ISIN", sorted(df["ISIN"].unique()))
        calc_date = st.date_input("Дата розрахунку")

    with col2:
        market = st.radio("Ринок для ціни з дохідності", ["Вторинний", "Первинний (Мінфін)"], horizontal=True)
        input_mode = st.radio("Що вводите?", ["Дохідність (%)", "Ціна (dirty)"], horizontal=True)

    with col3:
        if input_mode == "Дохідність (%)":
            y_val = st.number_input("Дохідність, %", value=10.0, step=0.01, format="%.2f")
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

                st.success(f"**Валютa:** {ccy}  •  **Номінальна ставка (купон):** {round(coupon_rate*100,2)}%")
                st.info(f"**Результат:** Dirty: **{dirty} {ccy}**  |  НКД: **{ai}**  |  Clean: **{clean}**  |  Формула: **{formula}**")

                st.markdown("**Графік купонів і погашення (від дати розрахунку):**")
                st.dataframe(sched, use_container_width=True)

            else:  # Ціна → Дохідності
                res = yields_from_price(str(calc_date), isin, p_val, df)
                st.success(
                    f"**Валютa:** {res['Currency']}  •  "
                    f"**Вторинний ринок:** {res['Secondary_yield']}% ({res['Secondary_formula']})  •  "
                    f"**Первинний (Мінфін):** {res['Primary_yield']}% ({res['Primary_formula']})"
                )
                # Для зручності додамо графік платежів
                sched, coupon_rate, ccy = build_cashflow_schedule(df, isin, from_date=str(calc_date))
                st.markdown(f"**Номінальна ставка (купон):** {round(coupon_rate*100,2)}%  •  **Валюта:** {ccy}")
                st.markdown("**Графік купонів і погашення (від дати розрахунку):**")
                st.dataframe(sched, use_container_width=True)

        except Exception as e:
            st.error(f"Помилка розрахунку: {e}")

# ================== TAB 2: P&L УГОДИ ==================
with tab_trade:
    st.subheader("Розрахувати угоду: Купив → Продав (P&L)")

    c1, c2 = st.columns(2)

    with c1:
        isin_t = st.selectbox("ISIN для угоди", sorted(df["ISIN"].unique()))
        buy_date = st.date_input("Дата покупки")
        buy_y = st.number_input("Дохідність покупки, % (вторинний ринок)", value=10.00, step=0.01, format="%.2f")

    with c2:
        sell_date = st.date_input("Дата продажу")
        sell_y = st.number_input("Дохідність продажу, % (вторинний ринок)", value=9.50, step=0.01, format="%.2f")

    if st.button("Порахувати P&L", type="primary"):
        try:
            res = trade_outcome(isin_t, str(buy_date), buy_y, str(sell_date), sell_y, df)

            st.success(f"**Валюта:** {res['Currency']}  •  **Тримали днів:** {res['Days_held']}")
            st.write(f"**Ціна покупки (dirty):** {res['Buy']['price_dirty']}  |  **Ціна продажу (dirty):** {res['Sell']['price_dirty']}")
            st.write(f"**Отримані купони:** {res['Coupons_total']}  |  **Прибуток (абс.):** {res['Profit_abs']}  |  **Річна проста %:** {res['Profit_ann_pct']}%")

            if res["Coupons_received"]:
                st.markdown("**Купони за період володіння:**")
                st.dataframe(pd.DataFrame(res["Coupons_received"], columns=["Дата", "Сума"]), use_container_width=True)
            else:
                st.info("За період володіння купонів не було.")

        except Exception as e:
            st.error(f"Помилка розрахунку P&L: {e}")
