import pandas as pd
import requests
import io
import streamlit as st
from pathlib import Path

URL = "https://bank.gov.ua/files/Fair_value/sec_hdbk.xls"
FALLBACK_PATH = Path("data/sec_hdbk_sample.xlsx")

@st.cache_data(ttl=86400)
def load_df():
    """
    Загружаем таблицу НБУ. Если сеть/формат подвели — используем локальный fallback.
    Возвращаем: df (нормализованный), date_actuality (date)
    """
    def _parse_excel(bytes_blob: bytes):
        # читаем «сыро» без хедера, чтобы динамически найти строку заголовков
        raw = io.BytesIO(bytes_blob)
        df_raw = pd.read_excel(raw, header=None)

        # ищем строку, где находится заголовок (там должен быть столбец 'ISIN')
        header_row, date_actuality = None, None
        for i in range(min(30, len(df_raw))):
            row = df_raw.iloc[i].astype(str).str.strip().tolist()
            if any(x.upper() == "ISIN" for x in row):
                header_row = i
            if date_actuality is None:
                # пробуем выдернуть дату из первых строк
                for token in row:
                    try:
                        maybe_date = pd.to_datetime(token, dayfirst=True, errors="raise")
                        date_actuality = maybe_date.date()
                        break
                    except Exception:
                        pass

        if header_row is None:
            raise ValueError("Не найден заголовок таблицы (строка с 'ISIN'). Формат НБУ изменился.")

        # перечитываем с корректной шапкой
        raw.seek(0)
        df = pd.read_excel(raw, header=header_row)

        if "ISIN" not in df.columns:
            raise ValueError("После парсинга отсутствует колонка 'ISIN'.")

        # нормализуем имена столбцов
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
        df = df.rename(columns={c: rename_map.get(c, c) for c in df.columns})

        # оставим ключевые поля (если есть)
        keep = [c for c in [
            "ISIN","Par_value","Coupon_per_year","Coupon_rate","Yield_nominal",
            "Date_Issue","Date_maturity","Currency","Instrument_type"
        ] if c in df.columns]
        df = df[keep].copy()

        # типизация
        for col in ["Par_value", "Coupon_per_year", "Coupon_rate", "Yield_nominal"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        for col in ["Date_Issue", "Date_maturity"]:
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors="coerce")

        # чистка
        df = df[df["ISIN"].notna()].drop_duplicates(subset=["ISIN"]).reset_index(drop=True)

        # дата актуальности — если не нашли в шапке, берем суррогат
        if date_actuality is None:
            if "Date_Issue" in df.columns and df["Date_Issue"].notna().any():
                date_actuality = pd.to_datetime(df["Date_Issue"].max()).date()
            else:
                date_actuality = pd.Timestamp.today().date()

        return df, date_actuality

    # 1) Пытаемся скачать с НБУ
    try:
        r = requests.get(URL, timeout=30)
        r.raise_for_status()
        df, asof = _parse_excel(r.content)
        return df, asof
    except Exception as e_web:
        st.warning(f"НБУ недоступен або формат змінився: {e_web}. Використовую локальний файл.", icon="⚠️")

        # 2) Фоллбек: локальный файл
        if not FALLBACK_PATH.exists():
            raise FileNotFoundError(
                f"Файл фоллбека не найден: {FALLBACK_PATH}. "
                f"Створи його у репозиторії (див. інструкцію)."
            )

        # читаем локальный .xlsx (здесь уже шапка должна быть «ровной»)
        df_local = pd.read_excel(FALLBACK_PATH)
        # приводим к тем же именам и типам (на случай, если отредактируешь вручную)
        rename_map = {
            "ISIN": "ISIN",
            "Par_value": "Par_value",
            "Coupon_per_year": "Coupon_per_year",
            "Coupon_rate": "Coupon_rate",
            "Yield_nominal": "Yield_nominal",
            "Date_Issue": "Date_Issue",
            "Date_maturity": "Date_maturity",
            "Currency": "Currency",
            "Instrument_type": "Instrument_type",
        }
        df_local = df_local.rename(columns={c: rename_map.get(c, c) for c in df_local.columns})

        # типизация/чистка точно так же
        for col in ["Par_value", "Coupon_per_year", "Coupon_rate", "Yield_nominal"]:
            if col in df_local.columns:
                df_local[col] = pd.to_numeric(df_local[col], errors="coerce")
        for col in ["Date_Issue", "Date_maturity"]:
            if col in df_local.columns:
                df_local[col] = pd.to_datetime(df_local[col], errors="coerce")
        df_local = df_local[df_local["ISIN"].notna()].drop_duplicates(subset=["ISIN"]).reset_index(drop=True)

        asof = pd.Timestamp.today().date()
        return df_local, asof
