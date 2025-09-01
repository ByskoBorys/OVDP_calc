import pandas as pd
import requests
import streamlit as st

URL = "https://bank.gov.ua/files/Fair_value/sec_hdbk.xls"

@st.cache_data(ttl=86400)
def load_df():
    # Завантаження файлу НБУ
    r = requests.get(URL, timeout=30)
    r.raise_for_status()
    with open("sec_hdbk.xls", "wb") as f:
        f.write(r.content)

    df = pd.read_excel("sec_hdbk.xls", header=None)

    # Дата актуальності (якщо є в перших рядках)
    try:
        if isinstance(df.iloc[1,0], str) and isinstance(df.iloc[1,1], str):
            date_text = df.iloc[1,0] + " " + df.iloc[1,1]
            date_actuality = date_text.split()[-1]
        else:
            date_actuality = "N/A"
    except Exception:
        date_actuality = "N/A"

    # Чистка таблиці
    df = df.iloc[4:].reset_index(drop=True)
    df = df.iloc[:, list(range(9)) + [-1]]
    df.columns = [
        "ISIN", "Type", "Currency", "Date_Issue", "Par_value",
        "Coupon_per_year", "Date_maturity", "Drop", "Yield_nominal", "qnt"
    ]
    df = df.drop(columns=["Drop"])
    df = df.drop(0, errors="ignore").reset_index(drop=True)
    df = df.drop_duplicates(subset=["ISIN"], keep="first")

    # Типи
    df["Par_value"] = pd.to_numeric(df["Par_value"], errors="coerce")
    df["Coupon_per_year"] = pd.to_numeric(df["Coupon_per_year"], errors="coerce")
    df["Yield_nominal"] = pd.to_numeric(df["Yield_nominal"], errors="coerce")
    df["Date_Issue"] = pd.to_datetime(df["Date_Issue"], errors="coerce")
    df["Date_maturity"] = pd.to_datetime(df["Date_maturity"], errors="coerce")

    # Прибрати порожні ISIN
    df = df[df["ISIN"].notna()].reset_index(drop=True)

    return df, date_actuality
