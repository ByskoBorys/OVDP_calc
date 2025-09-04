import io
from pathlib import Path
import re
import requests
import pandas as pd
import streamlit as st

# ---- источники ----
URL_XLS = "https://bank.gov.ua/files/Fair_value/sec_hdbk.xls"
FALLBACKS = [
    Path("data/sec.hdbk-2.xls"),
    Path("data/sec_hdbk_sample.xlsx"),
    Path("data/sec_hdbk_sample.csv"),
]

# ---- утилиты ----
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

def _parse_table_like_in_spec(df: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    """
    Реализация ровно по твоему алгоритму:
      - дата актуальности = A2 + ' ' + B2  (берём последним словом из склейки)
      - режем первые 4 строки
      - оставляем колонки A..I + последний столбец
      - переименовываем как в примере
      - дропаем лишнее, первую строку, приводим типы
    Возвращает: (нормализованный df, asof_label_str)
    """
    # 1) Дата актуальности
    date_actuality = "Не вдалося визначити дату"
    try:
        a2 = df.iloc[1, 0]
        b2 = df.iloc[1, 1]
        if isinstance(a2, str) and isinstance(b2, str):
            date_text = f"{a2} {b2}"
            # возьмем последний токен, но если это не дата — поищем шаблон dd.mm.yyyy
            token = date_text.split()[-1]
            m = re.search(r"(\d{2}\.\d{2}\.\d{4})", date_text)
            date_actuality = m.group(1) if m else token
    except Exception:
        pass

    # 2) срез строк
    df = df.iloc[4:].reset_index(drop=True)

    # 3) колонки: A..I (0..8) + последний (-1)
    keep_idx = list(range(9)) + [-1]
    # на всякий случай, если столбцов меньше
    keep_idx = [i for i in keep_idx if -len(df.columns) <= i < len(df.columns)]
    df = df.iloc[:, keep_idx]

    # 4) переименование 1-в-1 как у тебя
    new_cols = ["ISIN", "Type", "Currency", "Date_Issue", "Par_value",
                "Coupon_per_year", "Date_maturity", "Drop",
                "Yield_nominal", "qnt"]
    df.columns = new_cols[:len(df.columns)]

    # 5) выкинем H ("Drop")
    if "Drop" in df.columns:
        df = df.drop(columns=["Drop"], errors="ignore")

    # 6) удалить строку 0 (повтор заголовков)
    if not df.empty:
        df = df.drop(index=0, errors="ignore").reset_index(drop=True)

    # 7) типизация (ровно по смыслу твоих комментариев)
    if "Date_Issue" in df.columns:
        df["Date_Issue"] = pd.to_datetime(df["Date_Issue"], dayfirst=True, errors="coerce")
    if "Date_maturity" in df.columns:
        df["Date_maturity"] = pd.to_datetime(df["Date_maturity"], dayfirst=True, errors="coerce")

    if "Par_value" in df.columns:
        df["Par_value"] = _clean_num(df["Par_value"]).fillna(1000.0)

    if "Coupon_per_year" in df.columns:
        df["Coupon_per_year"] = _clean_num(df["Coupon_per_year"]).fillna(2).astype("Int64")
        df["Coupon_per_year"] = df["Coupon_per_year"].clip(lower=1)

    if "Yield_nominal" in df.columns:
        # в файле это проценты → переводим в десятичную долю
        df["Yield_nominal"] = _clean_num(df["Yield_nominal"]) / 100.0

    # 8) один ряд на ISIN
    if "ISIN" in df.columns:
        df = df[df["ISIN"].notna()].drop_duplicates(subset=["ISIN"], keep="first").reset_index(drop=True)

    # Дополнительные удобные поля для расчётов:
    # — Coupon_rate: используем номинальную доходность как купонную ставку (твоя логика)
    if "Yield_nominal" in df.columns and "Par_value" in df.columns:
        df["Coupon_rate"] = df["Yield_nominal"].fillna(0.0)  # десятичная ставка

    # Дефолты
    if "Currency" in df.columns:
        df["Currency"] = df["Currency"].fillna("UAH")
    else:
        df["Currency"] = "UAH"

    return df, date_actuality

def _read_xls_bytes_like_spec(content: bytes) -> tuple[pd.DataFrame, str]:
    raw = io.BytesIO(content)
    df_raw = pd.read_excel(raw, header=None, engine="xlrd")
    return _parse_table_like_in_spec(df_raw)

def _read_local_file_like_spec(path: Path) -> tuple[pd.DataFrame, str]:
    ext = path.suffix.lower()
    if ext == ".xls":
        df_raw = pd.read_excel(path, header=None, engine="xlrd")
    elif ext == ".xlsx":
        df_raw = pd.read_excel(path, header=None, engine="openpyxl")
    elif ext == ".csv":
        # попробуем эмулировать структуру: csv без заголовка
        df_raw = pd.read_csv(path, header=None)
    else:
        raise ValueError(f"Непідтримуваний формат: {path}")
    return _parse_table_like_in_spec(df_raw)

# ---- публичная функция ----
@st.cache_data(ttl=86400)
def load_df():
    """
    Возвращает (df, asof_label):
      df — по одному ряду на ISIN со столбцами: ISIN, Type, Currency, Date_Issue,
            Par_value, Coupon_per_year, Date_maturity, Yield_nominal (доля),
            qnt, Coupon_rate (доля)
      asof_label — дата актуальности из A2+B2 (если не вышло — дата удачной загрузки).
    """
    # 1) web
    try:
        r = requests.get(URL_XLS, timeout=30)
        r.raise_for_status()
        df, asof = _read_xls_bytes_like_spec(r.content)
        # если дату не удалось вытащить из файла — ставим дату успешного скачивания
        if not asof or "не вдал" in asof.lower():
            asof = str(pd.Timestamp.now().date())
        return df, asof
    except Exception as e_web:
        st.warning(f"НБУ недоступен або формат змінився: {e_web}. Використовую локальний файл.", icon="⚠️")

    # 2) fallbacks
    for p in FALLBACKS:
        if p.exists():
            try:
                df, asof = _read_local_file_like_spec(p)
                if not asof or "не вдал" in asof.lower():
                    asof = f"локальний файл • {pd.Timestamp.now().date()}"
                return df, asof
            except Exception as e_loc:
                st.warning(f"Не вдалося прочитати {p.name}: {e_loc}")

    raise FileNotFoundError(f"Немає жодного файлу фоллбека: {', '.join(str(p) for p in FALLBACKS)}")
