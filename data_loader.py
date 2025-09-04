import io
from pathlib import Path
import pandas as pd
import numpy as np
import requests
import streamlit as st

URL_XLS = "https://bank.gov.ua/files/Fair_value/sec_hdbk.xls"

FALLBACK_PATHS = [
    Path("data/sec.hdbk-2.xls"),
    Path("data/sec_hdbk_sample.xlsx"),
    Path("data/sec_hdbk_sample.csv"),
]

# ----------------------- helpers -----------------------

def _clean_percent_series(s: pd.Series) -> pd.Series:
    if s is None:
        return pd.Series(dtype=float)
    s2 = s.astype(str).str.replace("%", "", regex=False).str.replace(",", ".", regex=False).str.strip()
    s_num = pd.to_numeric(s2, errors="coerce")
    if s_num.dropna().median() > 1.0:
        s_num = s_num / 100.0
    return s_num

def _find_header_row(df_raw: pd.DataFrame) -> int | None:
    for i in range(min(40, len(df_raw))):
        row = df_raw.iloc[i].astype(str).str.strip().str.lower().tolist()
        if any(x == "isin" for x in row):
            return i
    return None

def _rename_initial(df: pd.DataFrame) -> pd.DataFrame:
    """Початкове маппінг-ренейм найтиповіших назв у канон."""
    mapping = {}
    lowmap = {c: str(c).lower() for c in df.columns}

    def find(*keys):
        for c, low in lowmap.items():
            if all(k in low for k in keys):
                return c
        return None

    m = find("isin");                            mapping[m] = "ISIN"                       if m else None
    m = find("валют", "випуск") or find("currency"); mapping[m] = "Currency"               if m else None
    m = find("дата", "випуск") or find("issue"); mapping[m] = "Date_Issue"                 if m else None
    m = find("номінальн", "варт") or find("par"); mapping[m] = "Par_value"                 if m else None
    m = find("кількість", "купон") or find("количество", "купон"); mapping[m] = "Coupon_per_year" if m else None
    m = find("дата", "погаш") or find("maturity"); mapping[m] = "Date_maturity"            if m else None
    m = find("тип", "виплат") or find("тип", "выплат"); mapping[m] = "Payment_type"        if m else None
    # Колонка I: «Номінальний рівень дохідності, %»
    # частіше за все містить слова 'номінальн' і 'дохідн' і '%'
    m = find("номінальн", "дохідн") or find("номинал", "доход"); mapping[m] = "Yield_nominal" if m else None

    # застосувати тільки знайдені ключі
    mapping = {k: v for k, v in mapping.items() if k is not None}
    return df.rename(columns=mapping) if mapping else df

def _detect_coupon_amount_column(df: pd.DataFrame) -> str | None:
    """
    Знайти колонку «сума купонного платежу на 1 облігацію».
    Логіка: серед числових/квазі-числових колонок шукаємо ту, де:
      - на рядках Payment_type ~ 'купон' медіана > 0
      - на рядках Payment_type ~ 'погаш' медіана близька до 0
    """
    if "Payment_type" not in df.columns:
        return None

    # перетворимо усі неканонічні колонки у numeric-кандидати
    canonical = {
        "ISIN","Currency","Date_Issue","Par_value","Coupon_per_year",
        "Date_maturity","Payment_type","Yield_nominal"
    }
    candidates = []
    pt = df["Payment_type"].astype(str).str.lower()
    mask_coupon = pt.str.contains("купон", na=False)
    mask_redemp = pt.str.contains("погаш", na=False)

    for c in df.columns:
        if c in canonical:
            continue
        series = pd.to_numeric(
            df[c].astype(str).str.replace(",", ".", regex=False).str.replace("%", "", regex=False),
            errors="coerce"
        )
        if series.notna().sum() < max(3, int(0.05 * len(series))):
            continue
        med_coupon = series[mask_coupon].median(skipna=True)
        med_redem  = series[mask_redemp].median(skipna=True)
        if pd.notna(med_coupon) and med_coupon > 0 and (pd.isna(med_redem) or abs(med_redem) < 1e-6):
            candidates.append((c, med_coupon, med_redem))

    # оберемо найбільш «виразну» колонку (найбільша медіана на купонах)
    if candidates:
        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[0][0]
    return None

def _aggregate_to_bond_directory(df: pd.DataFrame) -> pd.DataFrame:
    """
    З редукованої «подієвої» таблиці (купонні/погашення) робимо 1 рядок на ISIN.
    Рахуємо Coupon_rate як (coupon_amount_per_payment * freq)/Par.
    """
    coupon_col = _detect_coupon_amount_column(df)
    # приведемо типи
    if "Par_value" in df.columns:
        df["Par_value"] = pd.to_numeric(df["Par_value"], errors="coerce")
    if "Coupon_per_year" in df.columns:
        df["Coupon_per_year"] = pd.to_numeric(df["Coupon_per_year"], errors="coerce")
    if "Yield_nominal" in df.columns:
        df["Yield_nominal"] = _clean_percent_series(df["Yield_nominal"])
    for col in ["Date_Issue","Date_maturity"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")

    # Вибір «купонних» рядків
    coupon_rows = pd.Series(False, index=df.index)
    if "Payment_type" in df.columns:
        coupon_rows = df["Payment_type"].astype(str).str.lower().str.contains("купон", na=False)

    if coupon_col is not None:
        coupon_amount = pd.to_numeric(
            df[coupon_col].astype(str).str.replace(",", ".", regex=False),
            errors="coerce"
        )
    else:
        coupon_amount = pd.Series(np.nan, index=df.index)

    # групування
    groups = []
    for isin, g in df.groupby("ISIN"):
        if pd.isna(isin):
            continue
        row = {}
        row["ISIN"] = isin
        row["Currency"] = g["Currency"].dropna().iloc[0] if "Currency" in g.columns and g["Currency"].notna().any() else "UAH"
        row["Par_value"] = float(g["Par_value"].dropna().iloc[0]) if "Par_value" in g.columns and g["Par_value"].notna().any() else 1000.0
        row["Coupon_per_year"] = int(g["Coupon_per_year"].dropna().iloc[0]) if "Coupon_per_year" in g.columns and g["Coupon_per_year"].notna().any() else 2
        row["Date_Issue"] = g["Date_Issue"].dropna().min() if "Date_Issue" in g.columns else pd.NaT
        row["Date_maturity"] = g["Date_maturity"].dropna().max() if "Date_maturity" in g.columns else pd.NaT
        row["Instrument_type"] = g["Instrument_type"].dropna().iloc[0] if "Instrument_type" in g.columns and g["Instrument_type"].notna().any() else None
        row["Yield_nominal"] = float(g["Yield_nominal"].dropna().iloc[0]) if "Yield_nominal" in g.columns and g["Yield_nominal"].notna().any() else np.nan

        # купонна сума (на 1 облігацію) з рядків «купонний платіж»
        if coupon_col is not None and "Payment_type" in g.columns:
            gc = g.copy()
            mask_c = gc["Payment_type"].astype(str).str.lower().str.contains("купон", na=False)
            if mask_c.any():
                amt = pd.to_numeric(
                    gc.loc[mask_c, coupon_col].astype(str).str.replace(",", ".", regex=False),
                    errors="coerce"
                ).dropna()
                coupon_amt = float(amt.iloc[0]) if len(amt) else 0.0
            else:
                coupon_amt = 0.0
        else:
            coupon_amt = 0.0

        # ставка купона (десяткова), страхуємося від ділення на нуль
        if row["Par_value"] and row["Coupon_per_year"] and coupon_amt:
            row["Coupon_rate"] = (coupon_amt * row["Coupon_per_year"]) / row["Par_value"]
        else:
            row["Coupon_rate"] = 0.0

        # Якщо Date_Issue порожня — підставимо Maturity − 365 днів
        if pd.isna(row["Date_Issue"]) and pd.notna(row["Date_maturity"]):
            row["Date_Issue"] = row["Date_maturity"] - pd.to_timedelta(365, unit="D")

        groups.append(row)

    out = pd.DataFrame(groups)

    # Фінальна типізація/дефолти
    if "Par_value" in out.columns:
        out["Par_value"] = pd.to_numeric(out["Par_value"], errors="coerce").fillna(1000.0)
    if "Coupon_per_year" in out.columns:
        out["Coupon_per_year"] = pd.to_numeric(out["Coupon_per_year"], errors="coerce").fillna(2).clip(lower=1).astype(int)
    if "Coupon_rate" in out.columns:
        out["Coupon_rate"] = pd.to_numeric(out["Coupon_rate"], errors="coerce").fillna(0.0)
    if "Yield_nominal" in out.columns:
        out["Yield_nominal"] = pd.to_numeric(out["Yield_nominal"], errors="coerce")

    for col in ["Date_Issue","Date_maturity"]:
        if col in out.columns:
            out[col] = pd.to_datetime(out[col], errors="coerce")

    # Забезпечимо наявність мінімального набору колонок
    for col, default in [
        ("Currency","UAH"),
        ("Instrument_type", None),
        ("Coupon_rate", 0.0),
        ("Yield_nominal", np.nan),
    ]:
        if col not in out.columns:
            out[col] = default

    out = out[out["ISIN"].notna()].drop_duplicates(subset=["ISIN"]).reset_index(drop=True)
    return out

def _parse_web_xls(content: bytes) -> pd.DataFrame:
    raw = io.BytesIO(content)
    df_raw = pd.read_excel(raw, header=None, engine="xlrd")
    header_row = _find_header_row(df_raw)

    raw.seek(0)
    df = pd.read_excel(raw, engine="xlrd") if header_row is None else pd.read_excel(raw, header=header_row, engine="xlrd")
    df = _rename_initial(df)     # дає нам ISIN / Currency / Par_value / Coupon_per_year / Date_* / Payment_type / Yield_nominal
    df = _aggregate_to_bond_directory(df)
    return df

def _read_local(path: Path) -> pd.DataFrame:
    ext = path.suffix.lower()
    if ext == ".xls":
        df = pd.read_excel(path, engine="xlrd")
    elif ext == ".xlsx":
        df = pd.read_excel(path, engine="openpyxl")
    elif ext == ".csv":
        df = pd.read_csv(path)
    else:
        raise ValueError(f"Непідтримуваний формат фоллбека: {path}")

    df = _rename_initial(df)
    df = _aggregate_to_bond_directory(df)
    return df

# ----------------------- public -----------------------

@st.cache_data(ttl=86400)
def load_df():
    """
    Повертає: (df, asof_label)
    df — по ОДНОМУ рядку на ISIN зі стовпцями:
        ISIN, Currency, Par_value, Coupon_per_year, Coupon_rate (десяткова),
        Date_Issue, Date_maturity, Instrument_type, Yield_nominal (десяткова)
    asof_label — дата успішного завантаження (рядок для UI).
    """
    # 1) web
    try:
        r = requests.get(URL_XLS, timeout=30)
        r.raise_for_status()
        df = _parse_web_xls(r.content)
        asof_label = str(pd.Timestamp.now().date())
        if df["Date_maturity"].isna().all():
            raise ValueError("У джерелі НБУ відсутня коректна дата погашення.")
        return df, asof_label
    except Exception as e_web:
        st.warning(f"НБУ недоступен або формат змінився: {e_web}. Використовую локальний файл.", icon="⚠️")

    # 2) fallbacks
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
