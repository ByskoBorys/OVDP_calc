import io
from datetime import date

import pandas as pd
import streamlit as st

from data_loader import load_df
from bond_utils import (
    secondary_price_from_yield,
    primary_price_from_yield_minfin,
    yields_from_price,
    build_cashflow_schedule,
    trade_outcome,
)

st.set_page_config(page_title="ОВДП калькулятор", layout="wide")
st.title("📈 Калькулятор ОВДП")

# ---------- helper: xlsx export ----------
def _to_xlsx_bytes(sheets: dict) -> bytes:
    """
    sheets: {"SheetName": DataFrame або dict}
    Повертає байти XLSX для st.download_button.
    """
    bio = io.BytesIO()
    with pd.ExcelWriter(bio, engine="openpyxl") as writer:
        for name, data in sheets.items():
            sheet = name[:31] if name else "Sheet1"
            if isinstance(data, pd.DataFrame):
                data.to_excel(writer, sheet_name=sheet, index=False)
            elif isinstance(data, dict):
                pd.DataFrame(list(data.items()), columns=["Поле", "Значення"]).to_excel(
                    writer, sheet_name=sheet, index=False
                )
            else:
                pd.DataFrame([data]).to_excel(writer, sheet_name=sheet, index=False)
    bio.seek(0)
    return bio.getvalue()

# ---------- load data (web or fallback) ----------
with st.spinner("Завантажуємо дані НБУ..."):
    try:
        df, asof_label = load_df()  # asof_label — уже «красива» строка
    except Exception as e:
        st.error(f"Не вдалось завантажити дані НБУ: {e}")
        st.stop()

# ---------- sidebar: info + FAQ ----------
st.sidebar.success(f"Дані НБУ станом на: {asof_label}")
st.sidebar.caption("Джерело: bank.gov.ua/files/Fair_value/sec_hdbk.xls")

with st.sidebar.expander("FAQ / Що вміє калькулятор?"):
    st.markdown(
        """
**Калькулятор** переводить **ціну ↔ дохідність** для ОВДП.

- **Вторинний ринок (ціна):**  
  • Для **дисконтних** та **купонних з останнім купоном** — **формула СІМ**.  
  • Для **усіх інших купонних** — через **YTM** (ефективна дохідність).

- **Первинний ринок (ціна Мінфіну):**  
  • **Дисконтні** — **формула СІМ**.  
  • **Купонні** — **формула Мінфіну** (середньозважена дохідність розміщення).

- **Угода Купив → Продав (P&L):**  
  Розрахунок P&L з урахуванням **dirty-цін** на дати купівлі/продажу, **отриманих купонів**, 
  кількості днів володіння та **річної простої дохідності**.
        """
    )

# ---------- tabs ----------
tab_calc, tab_trade = st.tabs(["Калькулятор", "Розрахувати угоду"])

# ========================= КАЛЬКУЛЯТОР =========================
with tab_calc:
    st.subheader("Розрахунок ціни ↔ дохідності")

    c1, c2, c3, _ = st.columns([1.5, 1.2, 1.2, 0.8])

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
                    dirty, ai, clean, ccy, formula = secondary_price_from_yield(
                        str(calc_date), isin, y_val, df
                    )
                else:
                    dirty, ai, clean, ccy, formula = primary_price_from_yield_minfin(
                        str(calc_date), isin, y_val, df
                    )

                sched, coupon_rate, ccy2 = build_cashflow_schedule(
                    df, isin, from_date=str(calc_date)
                )
                st.success(
                    f"**Валюта:** {ccy} • **Купон (номінальна):** {round(coupon_rate*100, 2)}%"
                )
                st.info(
                    f"**Результат:** Dirty: **{dirty} {ccy}** | НКД: **{ai}** | Clean: **{clean}** | Формула: **{formula}**"
                )
                st.markdown("**Графік купонів та погашення (від дати розрахунку):**")
                st.dataframe(sched, use_container_width=True)

                # XLSX export
                xlsx_bytes = _to_xlsx_bytes(
                    {
                        "Inputs": pd.DataFrame(
                            [{"ISIN": isin, "Дата": str(calc_date), "Ринок": market, "Ввід": "Y%", "Y, %": y_val}]
                        ),
                        "Result": pd.DataFrame(
                            [{"Dirty": dirty, "НКД": ai, "Clean": clean, "Валюта": ccy, "Формула": formula}]
                        ),
                        "Schedule": sched,
                    }
                )
                st.download_button(
                    "⬇️ Завантажити XLSX",
                    data=xlsx_bytes,
                    file_name=f"OVDP_calc_{isin}_{calc_date}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )

            else:
                res = yields_from_price(str(calc_date), isin, p_val, df)
                st.success(
                    f"**Валюта:** {res.get('Currency')} • "
                    f"**Вторинний:** {res.get('Secondary_yield')}% ({res.get('Secondary_formula')}) • "
                    f"**Первинний (Мінфін):** {res.get('Primary_yield')}% ({res.get('Primary_formula')})"
                )
                sched, coupon_rate, ccy = build_cashflow_schedule(
                    df, isin, from_date=str(calc_date)
                )
                st.markdown(
                    f"**Купон (номінальна):** {round(coupon_rate*100, 2)}% • **Валюта:** {ccy}"
                )
                st.dataframe(sched, use_container_width=True)

                # XLSX export
                res_row = {
                    "ISIN": isin,
                    "Дата": str(calc_date),
                    "Валюта": res.get("Currency"),
                    "Втор., %": res.get("Secondary_yield"),
                    "Формула (втор.)": res.get("Secondary_formula"),
                    "Перв. (Мінфін), %": res.get("Primary_yield"),
                    "Формула (перв.)": res.get("Primary_formula"),
                }
                xlsx_bytes = _to_xlsx_bytes(
                    {
                        "Inputs": pd.DataFrame([{"ISIN": isin, "Ввід": "Ціна (dirty)", "Ціна": p_val}]),
                        "Result": pd.DataFrame([res_row]),
                        "Schedule": sched,
                    }
                )
                st.download_button(
                    "⬇️ Завантажити XLSX",
                    data=xlsx_bytes,
                    file_name=f"OVDP_calc_{isin}_{calc_date}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )

        except Exception as e:
            st.error(f"Помилка розрахунку: {e}")

# ========================= УГОДА =========================
with tab_trade:
    st.subheader("P&L угоди (купив → продав)")

    c1, c2 = st.columns(2)
    with c1:
        isin_t = st.selectbox("ISIN для угоди", sorted(df["ISIN"].dropna().unique()))
        buy_date = st.date_input("Дата покупки", value=date.today())
        buy_y = st.number_input("Дохідність покупки, % (вторинний)", value=10.00, step=0.01, format="%.2f")
    with c2:
        sell_date = st.date_input("Дата продажу", value=date.today())
        sell_y = st.number_input("Дохідність продажу, % (вторинний)", value=9.50, step=0.01, format="%.2f")

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
                st.dataframe(
                    pd.DataFrame(res["Coupons_received"], columns=["Дата", "Сума"]),
                    use_container_width=True
                )
            else:
                st.info("За період володіння купонів не було.")

            # XLSX export (trade)
            trade_sheet = pd.DataFrame([{
                "ISIN": isin_t,
                "Дата купівлі": str(buy_date),
                "Y купівлі, %": buy_y,
                "Дата продажу": str(sell_date),
                "Y продажу, %": sell_y,
                "Валюта": res["Currency"],
                "Днів у позиції": res["Days_held"],
                "Dirty (buy)": res["Buy"]["price_dirty"],
                "Dirty (sell)": res["Sell"]["price_dirty"],
                "Купони, всього": res["Coupons_total"],
                "P&L, сума": res["Profit_abs"],
                "P&L, річна проста, %": res["Profit_ann_pct"],
            }])
            sheets = {"Trade": trade_sheet}
            if res.get("Coupons_received"):
                sheets["Coupons"] = pd.DataFrame(res["Coupons_received"], columns=["Дата", "Сума"])
            xlsx_bytes = _to_xlsx_bytes(sheets)

            st.download_button(
                "⬇️ Завантажити XLSX (угода)",
                data=xlsx_bytes,
                file_name=f"OVDP_trade_{isin_t}_{buy_date}_{sell_date}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        except Exception as e:
            st.error(f"Помилка розрахунку P&L: {e}")
