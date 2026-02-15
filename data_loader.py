import io
import re
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

# Источник НБУ
URL_XLSX = "https://bank.gov.ua/files/Fair_value/sec_hdbk.xlsx"

# Фоллбеки (в таком порядке)
FALLBACKS = [
    Path("data/sec.hdbk-2.xlsx"),        # как просил
    Path("data/sec_hdbk_sample.xlsx"),
    Path("data/sec_hdbk_sample.csv"),
]

def _clean_num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(
        s.astype(str)
         .str.replace("\u00A0", "", regex=False)  # nbsp
         .str.replace(" ", "", regex=False)
         .str.replace(",", ".", regex=False)
         .str.replace("%", "", regex=False)
         .str.strip(),
        errors="coerce"
    )

def _parse_like_spec(df_raw: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    """
    Ровно по заданию:
    1) дата актуальности = A2 + B2 (берём dd.mm.yyyy, если есть)
    2) удаляем первые 4 строки
    3) оставляем колонки A..I + последний столбец (AV)
    4) жёстко переименовываем
    5) удаляем H ("Drop") и строку 0
    6) типизация: даты — dayfirst, числа чистим, Yield_nominal → доля
    7) один ряд на ISIN
    + добавляем Coupon_rate = Yield_nominal (доля)
    """
    # 1) дата актуальности
    asof = "Не удалось определить дату"
    try:
        a2, b2 = df_raw.iloc[1, 0], df_raw.iloc[1, 1]
        if isinstance(a2, str) and isinstance(b2, str):
            txt = f"{a2} {b2}"
            m = re.search(r"(\d{2}\.\d{2}\.\d{4})", txt)
            asof = m.group(1) if m else txt.split()[-1]
    except Exception:
        pass

    # 2) срез строк
    df = df_raw.iloc[4:].reset_index(drop=True)

    # 3) колонки: A..I (0..8) + последний (-1)
    keep_idx = list(range(9)) + [-1]
    keep_idx = [i for i in keep_idx if -len(df.columns) <= i < len(df.columns)]
    df = df.iloc[:, keep_idx]

    # 4) заголовки
    new_cols = ["ISIN", "Type", "Currency", "Date_Issue", "Par_value",
                "Coupon_per_year", "Date_maturity", "Drop",
                "Yield_nominal", "qnt"]
    df.columns = new_cols[:len(df.columns)]

    # 5) удалить H и первую строку (повтор шапки)
    df = df.drop(columns=["Drop"], errors="ignore")
    if not df.empty:
        df = df.drop(index=0, errors="ignore").reset_index(drop=True)

    # 6) типизация
    if "Date_Issue" in df.columns:
        df["Date_Issue"] = pd.to_datetime(df["Date_Issue"], dayfirst=True, errors="coerce")
    if "Date_maturity" in df.columns:
        df["Date_maturity"] = pd.to_datetime(df["Date_maturity"], dayfirst=True, errors="coerce")

    if "Par_value" in df.columns:
        df["Par_value"] = _clean_num(df["Par_value"]).fillna(1000.0)

    if "Coupon_per_year" in df.columns:
        df["Coupon_per_year"] = _clean_num(df["Coupon_per_year"]).fillna(2).clip(lower=1).astype("Int64")

    if "Yield_nominal" in df.columns:
        # в файле это проценты → в долю
        df["Yield_nominal"] = _clean_num(df["Yield_nominal"]) / 100.0

    # 7) один ряд на ISIN
    if "ISIN" in df.columns:
        df = df[df["ISIN"].notna()].drop_duplicates(subset=["ISIN"], keep="first").reset_index(drop=True)

    # Доп-поля
    # Купонная ставка = номинальная доходность (доля), как мы договорились
    if "Yield_nominal" in df.columns:
        df["Coupon_rate"] = df["Yield_nominal"].fillna(0.0)
    else:
        df["Coupon_rate"] = 0.0

    if "Currency" in df.columns:
        df["Currency"] = df["Currency"].fillna("UAH")
    else:
        df["Currency"] = "UAH"

    # Если нет Date_Issue — подставим Maturity − 365 дн (для устойчивости)
    if "Date_Issue" in df.columns and "Date_maturity" in df.columns:
        miss = df["Date_Issue"].isna() & df["Date_maturity"].notna()
        df.loc[miss, "Date_Issue"] = df.loc[miss, "Date_maturity"] - pd.to_timedelta(365, unit="D")

    return df, asof

def _read_xlsx_bytes(content: bytes) -> tuple[pd.DataFrame, str]:
    raw = io.BytesIO(content)
    df_raw = pd.read_excel(raw, header=None, engine="openpyxl")
    return _parse_like_spec(df_raw)

def _read_local_any(path: Path) -> tuple[pd.DataFrame, str]:
    ext = path.suffix.lower()
    if ext == ".xls":
        df_raw = pd.read_excel(path, header=None, engine="xlrd")
    elif ext == ".xlsx":
        df_raw = pd.read_excel(path, header=None, engine="openpyxl")
    elif ext == ".csv":
        df_raw = pd.read_csv(path, header=None)
    else:
        raise ValueError(f"Неподдерживаемый формат: {path}")
    return _parse_like_spec(df_raw)

# ----------------------- Публичная -----------------------

@st.cache_data(ttl=86400)
def load_df():
    """
    Возвращает: (df, asof_label)
    df содержит по одному ряду на ISIN: ISIN, Type, Currency, Date_Issue, Par_value,
    Coupon_per_year, Date_maturity, Yield_nominal (доля), qnt, Coupon_rate (доля).
    asof_label — дата актуальности из A2+B2 (если не удалось — дата успешной загрузки).
    """
    try:
        r = requests.get(URL_XLSX, timeout=30)
        r.raise_for_status()
        df, asof = _read_xlsx_bytes(r.content)
        if not asof or "не удалось" in asof.lower():
            asof = str(pd.Timestamp.now().date())
        if df["Date_maturity"].isna().all():
            raise ValueError("В источнике НБУ нет корректной даты погашения.")
        return df, asof
    except Exception as e_web:
        st.warning(f"НБУ недоступен или формат изменился: {e_web}. Использую локальный файл.", icon="⚠️")

    for p in FALLBACKS:
        if p.exists():
            try:
                df, asof = _read_local_any(p)
                if df["Date_maturity"].isna().all():
                    raise ValueError("В fallback-файле нет корректной даты погашения.")
                if not asof or "не удалось" in asof.lower():
                    asof = f"локальный файл • {pd.Timestamp.now().date()}"
                return df, asof
            except Exception as e_loc:
                st.warning(f"Не удалось прочитать {p.name}: {e_loc}")

    raise FileNotFoundError("Нет ни web-источника, ни рабочего локального fallback-файла.")
