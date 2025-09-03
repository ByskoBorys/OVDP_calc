import pandas as pd
import requests
import io
import streamlit as st

URL = "https://bank.gov.ua/files/Fair_value/sec_hdbk.xls"

@st.cache_data(ttl=86400)
def load_df():
    # 1) Скачиваем файл НБУ
    r = requests.get(URL, timeout=30)
    r.raise_for_status()

    # 2) Читаем как Excel из памяти
    raw = io.BytesIO(r.content)
    df_raw = pd.read_excel(raw, header=None)

    # 3) Нахождение “шапки” таблицы и дата актуальности
    # (НБУ периодически двигает строки — поэтому ищем динамически)
    header_row = None
    date_actuality = None
    for i in range(min(30, len(df_raw))):
        row = df_raw.iloc[i].astype(str).str.strip().tolist()
        if any(x.upper() == "ISIN" for x in row):
            header_row = i
        if date_actuality is None:
            # ищем что-то вроде "станом на 01.09.2025" или дату в первых строках
            joined = " ".join(row)
            # простая эвристика
            for token in row:
                try:
                    maybe_date = pd.to_datetime(token, dayfirst=True, errors="raise")
                    date_actuality = maybe_date.date()
                    break
                except Exception:
                    pass
    if header_row is None:
        raise ValueError("Не вдалося знайти заголовок таблиці (рядок з 'ISIN'). Формат файлу НБУ змінився?")

    df = pd.read_excel(raw, header=header_row)
    if "ISIN" not in df.columns:
        raise ValueError("Після парсингу немає колонки 'ISIN'. Перевір формат файлу НБУ.")

    # Минимальная нормализация, названия столбцов могут немного плавать — подстрахуемся:
    rename_map = {
        "ISIN": "ISIN",
        "Номінал": "Par_value",
        "Номинал": "Par_value",
        "Кількість купонів на рік": "Coupon_per_year",
        "Количество купонов в год": "Coupon_per_year",
        "Дата розміщення": "Date_Issue",
        "Дата размещения": "Date_Issue",
        "Дата погашення": "Date_maturity",
        "Дата погашения": "Date_maturity",
        "Купонна ставка, %": "Coupon_rate",
        "Купонная ставка, %": "Coupon_rate",
        "Валюта": "Currency",
        "Тип інструменту": "Instrument_type",
        "Тип инструмента": "Instrument_type",
        "Середньозважена дохідність розміщення, %": "Yield_nominal",
        "Средневзвешенная доходность размещения, %": "Yield_nominal",
    }
    # Применим rename без ошибок для неизвестных колонок
    df = df.rename(columns={c: rename_map.get(c, c) for c in df.columns})

    # Оставим ключевые поля, если они существуют
    keep = [c for c in [
        "ISIN","Par_value","Coupon_per_year","Coupon_rate","Yield_nominal",
        "Date_Issue","Date_maturity","Currency","Instrument_type"
    ] if c in df.columns]
    df = df[keep].copy()

    # Типизация
    for col in ["Par_value", "Coupon_per_year", "Coupon_rate", "Yield_nominal"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    for col in ["Date_Issue", "Date_maturity"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")

    # Чистка
    df = df[df["ISIN"].notna()].drop_duplicates(subset=["ISIN"]).reset_index(drop=True)

    # Если дату актуальности не нашли — возьмём макс дату размещения/погашения как суррогат
    if date_actuality is None:
        if "Date_Issue" in df.columns and df["Date_Issue"].notna().any():
            date_actuality = pd.to_datetime(df["Date_Issue"].max()).date()
        else:
            date_actuality = pd.Timestamp.today().date()

    return df, date_actuality
