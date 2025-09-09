import io
from datetime import date

import pandas as pd
import streamlit as st

from data_loader import load_df
from bond_utils import (
    secondary_price_from_yield,
    primary_price_from_yield_minfin,
    secondary_yield_from_price,
    primary_yield_from_price_minfin,
    build_cashflow_schedule,
    trade_outcome,
)

st.set_page_config(page_title="ОВДП калькулятор", layout="wide")
st.title("📈 Калькулятор ОВДП")

# Кнопка очистки кеша
if st.sidebar.button("🔁 Очистити кеш і перезавантажити"):
    st.cache_data.clear()
    st.rerun()

# Загрузка даних НБУ
with st.spinner("Завантажуємо дані НБУ..."):
    try:
        df, asof_label = load_df()
    except Exception as e:
        st.error(f"Не вдалось завантажити дані НБУ: {e}")
        st.stop()

# Сайдбар: дата та FAQ
st.sidebar.success(f"Дані НБУ станом на: {asof_label}")
with st.sidebar.expander("FAQ"):
    st.write(
        "- Переклад **ціна ↔ дохідність**.\n"
        "- **Вторинний**: дисконт/останній купон → SIM; купонні → YTM.\n"
        "- **Первинний (Мінфін)**: дисконтні → SIM; купонні → **формула Мінфіну з показником** DF=(1+y/k)^(Days/KDP0).\n"
        "- НКД рахуємо на піврічній сітці **182 дні**.\n"
        "- На даті погашення у графіку — **2 рядки**: купон і погашення окремо."
    )

# ---------- утиліти ----------

def _parse_decimal(text: str) -> float | None:
    if text is None:
        return None
    s = str(text).strip().replace(" ", "").replace("\u00A0", "")
    s = s.replace(",", ".")
    try:
        return float(s)
    except Exception:
        return None

def _xlsx_one_sheet(sections: list[tuple[str, pd.DataFrame]]) -> bytes:
    bio = io.BytesIO()
    with pd.ExcelWriter(bio, engine="openpyxl") as w:
        sheet = "OVDP"
        start = 0
        for title, dfsec in sections:
            # заголовок
            tdf = pd.DataFrame([[title]], columns=[" "])
            tdf.to_excel(w, sheet_name=sheet, index=False, header=False, startrow=start)
            start += 1
            # дані
            if dfsec is not None and not dfsec.empty:
                dfsec.to_excel(w, sheet_name=sheet, index=False, startrow=start)
                start += len(dfsec) + 2
            else:
                pd.DataFrame([["—"]]).to_excel(w, sheet_name=sheet, index=False, header=False, startrow=start)
                start += 2
    bio.seek(0)
    return bio.getvalue()

# ---------- UI ----------

tab_calc, tab_trade = st.tabs(["Калькулятор", "P&L угоди"])

# ===== КАЛЬКУЛЯТОР =====
with tab_calc:
    st.subheader("Розрахунок ціни ↔ дохідності")

    c1, c2, c3, c4 = st.columns([1.5, 1.1, 1.1, 1.1])
    with c1:
        isin = st.selectbox("ISIN", sorted(df["ISIN"].dropna().unique()))
        calc_date = st.date_input("Дата розрахунку", value=date.today())
    with c2:
        input_mode = st.radio("Режим", ["Дохідність → Ціна", "Ціна → Дохідність"], horizontal=False)
    with c3:
        market = st.radio("Ринок", ["Вторинний", "Первинний (Мінфін)"], horizontal=False)
    with c4:
        if input_mode == "Дохідність → Ціна":
            y_text = st.text_input("Дохідність, %", value="10,00", placeholder="напр. 19,75")
        else:
            p_text = st.text_input("Ціна (dirty)", value="1000,00", placeholder="напр. 985,50")

    st.caption("Порада: можна вводити як з комою, так і з крапкою. Натисніть «Розрахувати» для застосування.")

    if st.button("Розрахувати", type="primary"):
        try:
            if input_mode == "Дохідність → Ціна":
                y_val = _parse_decimal(y_text)
                if y_val is None:
                    st.error("Некоректна дохідність.")
                    st.stop()

                if market == "Вторинний":
                    dirty, ai, clean, ccy, formula = secondary_price_from_yield(str(calc_date), isin, y_val, df)
                else:
                    dirty, ai, clean, ccy, formula = primary_price_from_yield_minfin(str(calc_date), isin, y_val, df)

                sched, coupon_rate, ccy2 = build_cashflow_schedule(df, isin, str(calc_date))
                st.success(f"**Валюта:** {ccy} • **Купон (номінальна):** {round(coupon_rate*100, 2)}%")
                st.info(f"**Результат:** Dirty: **{dirty} {ccy}** | НКД: **{ai}** | Clean: **{clean}** | Формула: **{formula}**")

                if not sched.empty:
                    st.markdown("**Графік купонів та погашення (від дати розрахунку):**")
                    st.dataframe(sched, use_container_width=True)

                inputs_df = pd.DataFrame([{"ISIN": isin, "Дата": str(calc_date), "Ринок": market, "Ввід": "Y%", "Y, %": y_val}])
                result_df = pd.DataFrame([{"Dirty": dirty, "НКД": ai, "Clean": clean, "Валюта": ccy, "Формула": formula}])
                xlsx = _xlsx_one_sheet([
                    ("Вхідні дані", inputs_df),
                    ("Результат", result_df),
                    ("Графік (майбутні потоки)", sched),
                ])
                st.download_button("⬇️ Завантажити XLSX", data=xlsx,
                                   file_name=f"OVDP_calc_{isin}_{calc_date}.xlsx",
                                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

            else:
                p_val = _parse_decimal(p_text)
                if p_val is None:
                    st.error("Некоректна ціна.")
                    st.stop()

                if market == "Вторинний":
                    res = secondary_yield_from_price(str(calc_date), isin, p_val, df)
                else:
                    res = primary_yield_from_price_minfin(str(calc_date), isin, p_val, df)

                st.success(f"**Валюта:** {res['Currency']} • **Дохідність:** {res['Yield_percent']}% ({res['Formula']})")

                sched, coupon_rate, ccy = build_cashflow_schedule(df, isin, str(calc_date))
                st.markdown(f"**Купон (номінальна):** {round(coupon_rate*100, 2)}% • **Валюта:** {ccy}")
                if not sched.empty:
                    st.dataframe(sched, use_container_width=True)

                inputs_df = pd.DataFrame([{"ISIN": isin, "Дата": str(calc_date), "Ринок": market, "Ввід": "Ціна", "Ціна (dirty)": p_val}])
                result_df = pd.DataFrame([{"Дохідність, %": res["Yield_percent"], "Формула": res["Formula"], "Валюта": res["Currency"]}])
                xlsx = _xlsx_one_sheet([
                    ("Вхідні дані", inputs_df),
                    ("Результат", result_df),
                    ("Графік (майбутні потоки)", sched),
                ])
                st.download_button("⬇️ Завантажити XLSX", data=xlsx,
                                   file_name=f"OVDP_yield_{isin}_{calc_date}.xlsx",
                                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        except Exception as e:
            st.error(f"Помилка розрахунку: {e}")

# ===== P&L УГОДИ =====
with tab_trade:
    st.subheader("P&L угоди (купив → продав)")

    c1, c2 = st.columns(2)
    with c1:
        isin_t = st.selectbox("ISIN для угоди", sorted(df["ISIN"].dropna().unique()))
        buy_date = st.date_input("Дата покупки", value=date.today())
        buy_y_txt = st.text_input("Дохідність покупки, % (вторинний)", value="10,00")
    with c2:
        sell_date = st.date_input("Дата продажу", value=date.today())
        sell_y_txt = st.text_input("Дохідність продажу, % (вторинний)", value="9,50")

    if st.button("Порахувати P&L", type="primary"):
        try:
            buy_y = _parse_decimal(buy_y_txt)
            sell_y = _parse_decimal(sell_y_txt)
            if buy_y is None or sell_y is None:
                st.error("Перевірте значення дохідностей.")
                st.stop()

            res = trade_outcome(isin_t, str(buy_date), buy_y, str(sell_date), sell_y, df)

            st.success(f"**Валюта:** {res['Currency']} • **Тримали днів:** {res['Days_held']}")
            st.write(f"**Dirty (buy):** {res['Buy']['price_dirty']} | **Dirty (sell):** {res['Sell']['price_dirty']}")
            st.write(f"**Отримані купони (включно з погашенням номіналу, якщо було):** {res['Coupons_total']} | "
                     f"**P&L, сума:** {res['Profit_abs']} | **Річна проста %:** {res['Profit_ann_pct']}%")

            if res.get("Coupons_received"):
                st.markdown("**Купони/погашення за період володіння:**")
                st.dataframe(pd.DataFrame(res["Coupons_received"], columns=["Дата", "Сума"]), use_container_width=True)
            else:
                st.info("За період володіння купонів не було.")

            trade_df = pd.DataFrame([{
                "ISIN": isin_t, "Дата купівлі": str(buy_date), "Y купівлі, %": buy_y,
                "Дата продажу": str(sell_date), "Y продажу, %": sell_y, "Валюта": res["Currency"],
                "Днів у позиції": res["Days_held"], "Dirty (buy)": res["Buy"]["price_dirty"],
                "Dirty (sell)": res["Sell"]["price_dirty"], "Купони, всього": res["Coupons_total"],
                "P&L, сума": res["Profit_abs"], "P&L, річна проста, %": res["Profit_ann_pct"],
            }])

            # Один аркуш XLSX
            xlsx = _xlsx_one_sheet([
                ("Trade", trade_df),
                ("Coupons in holding period", pd.DataFrame(res["Coupons_received"], columns=["Дата","Сума"]) if res.get("Coupons_received") else pd.DataFrame()),
            ])
            st.download_button("⬇️ Завантажити XLSX (угода)", data=xlsx,
                               file_name=f"OVDP_trade_{isin_t}_{buy_date}_{sell_date}.xlsx",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        except Exception as e:
            st.error(f"Помилка розрахунку P&L: {e}")
