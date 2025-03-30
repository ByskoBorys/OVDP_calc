code = '''
import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
import requests

# Функция расчета цены облигации
def calculate_bond_price(calc_date, isin, yield_percent, df):
    calc_date = pd.to_datetime(calc_date)
    bond = df[df["ISIN"] == isin]
    if bond.empty:
        return "Ошибка: ISIN не найден"

    bond = bond.iloc[0]
    maturity_date = pd.to_datetime(bond["Date_maturity"])
    coupon_rate = bond["Yield_nominal"] / 100
    par_value = bond["Par_value"]
    currency = bond["Currency"]
    issue_date = pd.to_datetime(bond["Date_Issue"])
    days_to_maturity = (maturity_date - calc_date).days
    yield_decimal = yield_percent / 100

    coupon_schedule = []
    future_coupons = []
    coupon_payment = round(par_value * coupon_rate / 2, 2)
    payment_date = maturity_date
    while payment_date >= issue_date:
        coupon_schedule.insert(0, (payment_date.strftime('%d/%m/%Y'), coupon_payment))
        if payment_date >= calc_date:
            future_coupons.insert(0, (payment_date, coupon_payment))
        payment_date -= timedelta(days=182)

    if days_to_maturity <= 182:
        price = (par_value + coupon_payment) / (1 + yield_decimal * (days_to_maturity / 365))
    elif len(future_coupons) == 1 and future_coupons[0][0] == maturity_date:
        price = (par_value + coupon_payment) / (1 + yield_decimal * (days_to_maturity / 365))
    else:
        price = sum([
            cp / (1 + yield_decimal) ** (((date - calc_date).days) / 365)
            for date, cp in future_coupons
        ])
        price += par_value / (1 + yield_decimal) ** (days_to_maturity / 365)

    return round(price, 2)

# Streamlit UI
st.title("Калькулятор ОВДП")

url = "https://bank.gov.ua/files/Fair_value/sec_hdbk.xls"
st.write("Загрузка данных с сайта НБУ...")
response = requests.get(url)
with open("sec_hdbk.xls", "wb") as file:
    file.write(response.content)
df = pd.read_excel("sec_hdbk.xls", header=None)

if isinstance(df.iloc[1, 0], str) and isinstance(df.iloc[1, 1], str):
    date_text = df.iloc[1, 0] + " " + df.iloc[1, 1]
    date_actuality = date_text.split(" ")[-1]
else:
    date_actuality = "Не удалось определить дату"

st.write(f"Дата актуальности файла: {date_actuality}")
df = df.iloc[4:].reset_index(drop=True)
df = df.iloc[:, list(range(9)) + [-1]]
df.columns = ["ISIN", "Type", "Currency", "Date_Issue", "Par_value", "Coupon_per_year", "Date_maturity", "Drop", "Yield_nominal", "qnt"]
df = df.drop(columns=["Drop"])
df = df.drop_duplicates(subset=["ISIN"], keep='first')
df = df.drop(0).reset_index(drop=True)

isin = st.selectbox("Выберите ISIN", df["ISIN"].unique())
calc_date = st.date_input("Дата расчета", datetime.today())
yield_input = st.number_input("Введите доходность, %", min_value=0.0, step=0.01)

if st.button("Рассчитать цену"):
    price = calculate_bond_price(calc_date, isin, yield_input, df)
    st.success(f"Рассчитанная цена: {price}")

'''

with open("app.py", "w") as f:
    f.write(code)