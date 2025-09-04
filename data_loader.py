import io
from pathlib import Path
import pandas as pd
import requests
import streamlit as st

# Web-джерело НБУ (XLS)
URL_XLS = "https://bank.gov.ua/files/Fair_value/sec_hdbk.xls"

# Локальні фоллбеки (в такому порядку)
FALLBACK_PATHS = [
    Path("data/sec.hdbk-2.xls"),        # як просив
    Path("data/sec_hdbk_sample.xlsx"),  # запасний варіант (xlsx)
    Path("data/sec_hdbk_sample.csv"),   # ще один запасний (csv)
]

# ----------------------- ВСПОМОГАТЕЛЬНЫЕ -----------------------

def _clean_percent_series(s: pd.Series) -> pd.Series:
    """
    Перетворює стовпець зі значеннями на кшталт '16%', '16,00', '0.16' у десятковий вигляд:
      16%  -> 0.16
      16   -> 0.16 (бо >1 => ділимо на 100)
      0.16 -> 0.16
    """
    if s is None:
        return pd.Series(dtype=float)
    s2 = s.astype(str).str.replace("%", "", regex=False).str.replace(",", ".", regex=False).str.strip()
    s_num = pd.to_numeric(s2, errors="coerce")
    # якщо медіана > 1 — значить це відсотки, поділимо на 100
    if s_num.dropna().median() > 1.0:
        s_num = s_num / 100.0
    return s_num

def _guess_and_rename(df: pd.DataFrame) -> pd.DataFrame:
    """
    Приводим «плавающие» названия к канону, типизируем,
    страхуемся по Par_value=1000, Coupon_per_year=2, Coupon_rate=0 (з парсером відсотків),
    і гарантуємо наявність Date_Issue (якщо нема — = Maturity − 365 дн).
    """
    def pick(cols, keys):
        for c in cols:
            low = str(c).lower()
            for k in keys:
                if k in low:
                    return c
        return None

    cols = list(df.columns)
    mapping = {}

    # ISIN
    c = pick(cols, ["isin"])
    if c: mapping[c] = "ISIN"

    # Nominal / Par value
    c = pick(cols, ["номінал", "номинал", "par", "номин.", "номін."])
    if c: mapping[c] = "Par_value"

    # Coupons per year
    c = pick(cols, ["кількість купон", "количество купон", "per year", "coupons"])
    if c: mapping[c] = "Coupon_per_year"

    # Coupon rate
    c = pick(cols, ["купонна ставка", "купонная ставка", "coupon", "rate"])
    if c: mapping[c] = "Coupon_rate"

    # Dates
    c = pick(cols, ["дата розміщ", "дата размещ", "issue"])
    if c: mapping[c] = "Date_Issue"
    c = pick(cols, ["дата погаш", "maturity"])
    if c: mapping[c] = "Date_maturity"

    # Currency / Instrument type
    c = pick(cols, ["валюта", "currency"])
    if c: mapping[c] = "Currency"
    c = pick(cols, ["тип інструмент", "тип инструмента", "instrument"])
    if c: mapping[c] = "Instrument_type"

    # Weighted yield at placement (MinFin nominal)
    c = pick(cols, ["середньозважена", "средневзвешенная", "yield_nominal", "розміщення", "размещения"])
    if c: mapping[c] = "Yield_nominal"

    if mapping:
        df = df.rename(columns=mapping)

    # Залишаємо ключові поля, якщо вони є
    keep = [c for c in [
        "ISIN","Par_value","Coupon_per_year","Coupon_rate","Yield_nominal",
        "Date_Issue","Date_maturity","Currency","Instrument_type"
    ] if c in df.columns]
    if keep:
        df = df[keep].copy()

    # Числові
    if "Par_value" in df.columns:
        df["Par_value"] = pd.to_numeric(df["Par_value"], errors="coerce")
    if "Coupon_per_year" in df.columns:
        df["Coupon_per_year"] = pd.to_numeric(df["Coupon_per_year"], errors="coerce")

    # Відсотки: robust парсинг у ДЕсятковий вигляд
    if "Coupon_rate" in df.columns:
        df["Coupon_rate"] = _clean_percent_series(df["Coupon_rate"])
    if "Yield_nominal" in df.columns:
        df["Yield_nominal"] = _clean_percent_series(df["Yield_nominal"])

    # Дати
    for col in ["Date_Issue", "Date_maturity"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")

    # Дефолти/гарантії
    if "Par_value" not in df.columns:
        df["Par_value"] = 1000
    df["Par_value"] = df["Par_value"].fillna(1000)

    if "Coupon_per_year" not in df.columns:
        df["Coupon_per_year"] = 2
    df["Coupon_per_year"] = df["Coupon_per_year"].fillna(2).clip(lower=1)

    if "Coupon_rate" not in df.columns:
        df["Coupon_rate"] = 0.0
    df["Coupon_rate"] = df["Coupon_rate"].fillna(0.0)

    if "Currency" not in df.columns:
        df["Currency"] = "UAH"
    df["Currency"] = df["Currency"].fillna("UAH")

    if "Date_maturity" not in df.columns:
        df["Date_maturity"] = pd.NaT
    if "Date_Issue" not in df.columns:
        df["Date_Issue"] = pd.NaT

    # Якщо Date_Issue пуст — оцінюємо як Maturity − 365 днів (для НКД/періоду)
    mask_issue = df["Date_Issue"].isna() & df["Date_maturity"].notna()
    df.loc[mask_issue, "Date_Issue"] = df.loc[mask_issue, "Date_maturity"] - pd.to_timedelta(365, unit="D")

    # Чистка
    if "ISIN" in df.columns:
        df = df[df["ISIN"].notna()].drop_duplicates(subset=["ISIN"]).reset_index(drop=True)

    return df


def _parse_web_xls(content: bytes) -> pd.DataFrame:
    raw = io.BytesIO(content)
    # «черновой» прохід для пошуку рядка заголовків
    df_raw = pd.read_excel(raw, header=None, engine="xlrd")
    header_row = None
    for i in range(min(30, len(df_raw))):
        row = df_raw.iloc[i].astype(str).str.strip().tolist()
        if any(x.strip().upper() == "ISIN" for x in row):
            header_row = i
            break

    raw.seek(0)
    if header_row is None:
        df = pd.read_excel(raw, engine="xlrd")
    else:
        df = pd.read_excel(raw, header=header_row, engine="xlrd")

    return _guess_and_rename(df)


def _read_local(path: Path) -> pd.DataFrame:
    ext = path.suffix.lower()
    if ext == ".xls":
        return _guess_and_rename(pd.read_excel(path, engine="xlrd"))
    if ext == ".xlsx":
        return _guess_and_rename(pd.read_excel(path, engine="openpyxl"))
    if ext == ".csv":
        return _guess_and_rename(pd.read_csv(path))
    raise ValueError(f"Неподдерживаемый формат фоллбека: {path}")

# ----------------------- ПУБЛИЧНАЯ ФУНКЦИЯ -----------------------

@st.cache_data(ttl=86400)
def load_df():
    """
    Возвращает: (df, asof_label)
    asof_label — строка для UI (дата успешной загрузки: web или локальний).
    """
    # 1) web
    try:
        r = requests.get(URL_XLS, timeout=30)
        r.raise_for_status()
        df = _parse_web_xls(r.content)
        asof_label = str(pd.Timestamp.now().date())  # дата успішного скачування
        if df["Date_maturity"].isna().all():
            raise ValueError("У джерелі НБУ відсутня коректна дата погашення.")
        return df, asof_label
    except Exception as e_web:
        st.warning(f"НБУ недоступен або формат змінився: {e_web}. Використовую локальний файл.", icon="⚠️")

    # 2) локальні фоллбеки
    for p in FALLBACK_PATHS:
        if p.exists():
            try:
                df = _read_local(p)
                if df["Date_maturity"].isna().all():
                    raise ValueError("У фоллбек-файлі немає коректної дати погашення.")
                asof_label = f"локальний файл • {pd.Timestamp.now().date()}"
                return df, asof_label
            except Exception as e_loc:
                st.warning(f"Не вдалося прочитати {p.name}: {e_loc}")

    raise FileNotFoundError(
        f"Немає жодного файлу фоллбека: {', '.join(str(p) for p in FALLBACK_PATHS)}"
    )
