# bond_utils.py
import pandas as pd
import numpy as np

DAY_COUNT = 365.0
STEP_DAYS = 182  # суворий піврічний крок для купонних дат

# ============================ БАЗА ============================

def _norm_pct_scalar(v) -> float:
    """0.16 из '16'/'16%'/'0.16'."""
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return 0.0
        if isinstance(v, str):
            s = v.replace("%", "").replace(",", ".").strip()
            x = float(s)
        else:
            x = float(v)
        return x/100.0 if x > 1.0 else x
    except Exception:
        return 0.0

def _get_bond_row(df: pd.DataFrame, isin: str) -> pd.Series:
    row = df.loc[df["ISIN"] == isin]
    if row.empty:
        raise KeyError(f"ISIN не знайдено: {isin}")
    row = row.iloc[0].copy()

    row["Par_value"] = float(row.get("Par_value", 1000) if pd.notna(row.get("Par_value", np.nan)) else 1000)

    k = row.get("Coupon_per_year", 2)
    try:
        k = int(k) if pd.notna(k) else 2
    except Exception:
        k = 2
    row["Coupon_per_year"] = max(1, k)

    coup = row.get("Coupon_rate", np.nan)
    if pd.isna(coup) or float(coup) == 0.0:
        coup = row.get("Yield_nominal", np.nan)
    row["Coupon_rate"] = _norm_pct_scalar(coup) if isinstance(coup, str) else (float(coup) if pd.notna(coup) else 0.0)

    row["Currency"] = str(row.get("Currency", "UAH")) if pd.notna(row.get("Currency", np.nan)) else "UAH"

    maturity = pd.to_datetime(row.get("Date_maturity", pd.NaT), errors="coerce")
    issue = pd.to_datetime(row.get("Date_Issue", pd.NaT), errors="coerce")
    if pd.isna(maturity):
        raise KeyError("У довіднику відсутня коректна Date_maturity.")
    if pd.isna(issue):
        issue = maturity - pd.to_timedelta(365, unit="D")

    row["Date_maturity"] = maturity.normalize()
    row["Date_Issue"] = issue.normalize()
    return row

# ======================= КУПОННІ ДАТИ (182) =======================

def _coupon_dates_182_from_maturity(maturity: pd.Timestamp, until_date: pd.Timestamp) -> list[pd.Timestamp]:
    """maturity, maturity-182, maturity-2*182, ... (повертає ВОЗР. список)."""
    d = maturity.normalize()
    desc = [d]
    for _ in range(120):  # safety
        d = d - pd.Timedelta(days=STEP_DAYS)
        desc.append(d.normalize())
        if d <= until_date:
            break
    asc = sorted(set(desc))
    return asc

def _semi_coupon_amount(par: float, coupon_rate: float, k: int) -> float:
    """Розмір купона на один період. Для нашої сітки (182) очікуємо k=2."""
    if coupon_rate <= 0 or k <= 0:
        return 0.0
    return par * coupon_rate / float(k)

# ========================== AI / ГРАФІК ==========================

def accrued_interest(calc_date, isin, df) -> float:
    """AI = SD * (days_since / KDP0), де KDP0 = (next - prev) у днях."""
    row = _get_bond_row(df, isin)
    par, y_coup, k = float(row["Par_value"]), float(row["Coupon_rate"]), int(row["Coupon_per_year"])
    if y_coup <= 0 or k <= 0:
        return 0.0

    calc = pd.to_datetime(calc_date).normalize()
    dates = _coupon_dates_182_from_maturity(row["Date_maturity"], calc)
    # знайти next/prev
    future = [d for d in dates if d > calc]
    next_c = future[0] if future else dates[-1]
    idx_next = dates.index(next_c)
    if idx_next == 0:
        approx_step = (dates[1] - dates[0]).days if len(dates) >= 2 else max(1, 365 // k)
        prev_c = next_c - pd.Timedelta(days=approx_step)
    else:
        prev_c = dates[idx_next - 1]

    kdp0 = (next_c - prev_c).days
    if kdp0 <= 0:
        kdp0 = max(1, 365 // k)

    sd = _semi_coupon_amount(par, y_coup, k)
    days_since = (calc - prev_c).days
    ai = 0.0 if kdp0 <= 0 else sd * max(0.0, days_since / kdp0)
    return float(round(ai, 8))

def build_cashflow_schedule(df: pd.DataFrame, isin: str, from_date: str):
    """
    Майбутні потоки від дати розрахунку.
    На даті погашення — ДВА рядки: спочатку «Купон», потім «Погашення».
    """
    row = _get_bond_row(df, isin)
    calc = pd.to_datetime(from_date).normalize()
    dates = _coupon_dates_182_from_maturity(row["Date_maturity"], calc)
    par, ccy, y_coup, k = float(row["Par_value"]), row["Currency"], float(row["Coupon_rate"]), int(row["Coupon_per_year"])
    sd = _semi_coupon_amount(par, y_coup, k)

    out = []
    for d in dates:
        if d <= calc:
            continue
        if d == dates[-1]:
            if sd > 0:
                out.append((d.strftime("%Y-%m-%d"), round(sd, 8), "Купон"))
            out.append((d.strftime("%Y-%m-%d"), round(par, 8), "Погашення"))
        else:
            if sd > 0:
                out.append((d.strftime("%Y-%m-%d"), round(sd, 8), "Купон"))
    sched = pd.DataFrame(out, columns=["Дата", "Сума", "Подія"])
    return sched, y_coup, ccy

# ===================== МІНФІН (price ← yield) =====================

def _full_coupon_schedule_and_params(df, isin):
    """
    Повертає (dates, par, ccy, coupon_rate, k, maturity) для Мінфіну.
    dates — купонні/фінальна дата у ВОЗР. порядку (сітка 182).
    """
    row = _get_bond_row(df, isin)
    par, ccy, y_coup, k, maturity = float(row["Par_value"]), row["Currency"], float(row["Coupon_rate"]), int(row["Coupon_per_year"]), row["Date_maturity"]
    # для узгодженості — генеруємо дати до «сьогодні» (ми все одно відфільтруємо за calc_date)
    dates = _coupon_dates_182_from_maturity(maturity, pd.Timestamp("1900-01-01"))
    return dates, par, ccy, y_coup, k, maturity

def calculate_price_minfin(calc_date, isin, yield_percent, df):
    """
    Єдиний вхід (з твого еталону):
      • купонні → Мінфін із показником: DF = (1 + y/k) ** (Days / KDP0)
      • дисконтні → SIM: Price = Nom / (1 + y * Days/365)
    Вихід: dirty, AI, clean, currency
    """
    calc_date = pd.to_datetime(calc_date).normalize()
    dates, par, ccy, coupon_rate, k, maturity = _full_coupon_schedule_and_params(df, isin)
    y = float(yield_percent) / 100.0

    if calc_date >= maturity:
        return 0.0, 0.0, 0.0, ccy

    # Дисконтна → SIM
    if (coupon_rate is None or coupon_rate <= 0) or (k is None or k <= 0):
        days = (maturity - calc_date).days
        dirty = round(par / (1.0 + y * (days / 365.0)), 2)
        AI = 0.0
        clean = dirty
        return dirty, AI, clean, ccy

    # Купонна → Мінфін (з показником)
    SD = par * coupon_rate / k

    future = [d for d in dates if d >= calc_date]
    if not future:
        return 0.0, 0.0, 0.0, ccy

    next_coupon = future[0]
    idx_next = dates.index(next_coupon)
    if idx_next == 0:
        approx_step = (dates[1] - dates[0]).days if len(dates) >= 2 else max(1, 365 // k)
        prev_coupon = next_coupon - pd.Timedelta(days=approx_step)
    else:
        prev_coupon = dates[idx_next - 1]

    KDP0 = (next_coupon - prev_coupon).days
    if KDP0 <= 0:
        KDP0 = max(1, 365 // k)

    base = 1.0 + y / k
    dirty = 0.0
    for d in future:
        DD = (d - calc_date).days
        DF = base ** (DD / KDP0)
        # купон
        dirty += SD / DF
        # номінал у дату погашення
        if d == maturity:
            dirty += par / DF

    dirty = round(dirty, 2)
    AI = accrued_interest(calc_date, isin, df)
    clean = round(dirty - AI, 2)
    return dirty, AI, clean, ccy

def primary_price_from_yield_minfin(calc_date: str, isin: str, yield_percent: float, df: pd.DataFrame):
    """Обгортка для UI: Мінфін (з показником) або SIM (дисконт)."""
    dirty, ai, clean, ccy = calculate_price_minfin(calc_date, isin, yield_percent, df)
    formula = "Мінфін (з показником)" if ai is not None else "SIM (дисконт, первинний)"
    return dirty, ai, clean, ccy, formula

# ===================== МІНФІН (yield ← price) =====================

def _solve_bisect(func, lo, hi, tol=1e-10, max_iter=200):
    f_lo, f_hi = func(lo), func(hi)
    if f_lo == 0: return lo
    if f_hi == 0: return hi
    if f_lo * f_hi > 0:
        for _ in range(60):
            hi *= 1.5
            f_hi = func(hi)
            if f_lo * f_hi <= 0: break
    for _ in range(max_iter):
        mid = 0.5*(lo+hi)
        f_mid = func(mid)
        if abs(f_mid) < tol: return mid
        if f_lo * f_mid <= 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return 0.5*(lo+hi)

def primary_yield_from_price_minfin(calc_date: str, isin: str, price_dirty: float, df: pd.DataFrame):
    """
    Інверсія Мінфіну (та ж формула, що в calculate_price_minfin):
      Price(y) = Σ SD / (1 + y/k)^(DD/KDP0) + Par / (1 + y/k)^(Dm/KDP0)
      Знаходимо y (у десяткових), повертаємо %.
    Для дисконтних → SIM інверсія.
    """
    calc = pd.to_datetime(calc_date).normalize()
    dates, par, ccy, coupon_rate, k, maturity = _full_coupon_schedule_and_params(df, isin)

    if coupon_rate <= 0 or k <= 0:
        # SIM інверсія
        days = (maturity - calc).days
        t = max(0.0, days / DAY_COUNT)
        y = (par / price_dirty - 1.0) / t if t > 0 else 0.0
        return {"Currency": ccy, "Yield_percent": round(y * 100.0, 2), "Formula": "SIM (інверсія, дисконт)"}

    # Підготовка next/prev для KDP0 (як у прямому)
    future = [d for d in dates if d >= calc]
    if not future:
        return {"Currency": ccy, "Yield_percent": 0.0, "Formula": "Мінфін (інверсія) — немає потоків"}
    next_coupon = future[0]
    idx_next = dates.index(next_coupon)
    if idx_next == 0:
        approx_step = (dates[1] - dates[0]).days if len(dates) >= 2 else max(1, 365 // k)
        prev_coupon = next_coupon - pd.Timedelta(days=approx_step)
    else:
        prev_coupon = dates[idx_next - 1]

    KDP0 = (next_coupon - prev_coupon).days
    if KDP0 <= 0:
        KDP0 = max(1, 365 // k)

    SD = par * coupon_rate / k

    def price_given_y(y: float) -> float:
        base = 1.0 + y / k
        pv = 0.0
        for d in future:
            DD = (d - calc).days
            DF = base ** (DD / KDP0)
            pv += SD / DF
            if d == maturity:
                pv += par / DF
        return pv

    y = _solve_bisect(lambda yy: price_given_y(yy) - price_dirty, 1e-10, 5.0)
    return {"Currency": ccy, "Yield_percent": round(y * 100.0, 2), "Formula": "Мінфін solve (з показником)"}

# ===================== ВТОРИННИЙ РИНОК =====================

def _sim_price(calc: pd.Timestamp, red_date: pd.Timestamp, red_amt: float, y: float) -> float:
    """Simple interest: PV = FV / (1 + y * t)."""
    days = (red_date - calc).days
    t = max(0.0, days / DAY_COUNT)
    return red_amt / (1.0 + y * t)

def _ytm_dirty(calc: pd.Timestamp, cfs: list, y: float) -> float:
    """Ефективна ставка: Σ CF / (1+y)^t."""
    pv = 0.0
    for d, amt in cfs:
        t = max(0.0, (d - calc).days / DAY_COUNT)
        pv += amt / ((1.0 + y) ** t)
    return pv

def secondary_price_from_yield(calc_date: str, isin: str, yield_percent: float, df: pd.DataFrame):
    """
    Вторинний:
      - дисконтні/останній купон → SIM,
      - купонні → YTM (ефективна).
    """
    row = _get_bond_row(df, isin)
    calc = pd.to_datetime(calc_date).normalize()
    par, ccy, y_coup, k, maturity = float(row["Par_value"]), row["Currency"], float(row["Coupon_rate"]), int(row["Coupon_per_year"]), row["Date_maturity"]
    dates = _coupon_dates_182_from_maturity(maturity, calc)
    sd = _semi_coupon_amount(par, y_coup, k)

    # AI (незалежно від двигуна)
    # (ми вже маємо функцію accrued_interest)
    ai = accrued_interest(calc, isin, df)

    y = float(yield_percent) / 100.0
    # скільки платежів залишилось?
    remain = sum(1 for d in dates if d > calc)
    if sd == 0.0 or remain <= 1:
        dirty = _sim_price(calc, maturity, par + (sd if sd > 0 else 0.0), y)
        formula = "SIM (дисконт/останній купон)"
    else:
        # CF окремо: купони + номінал у кінці
        cfs = []
        for d in dates:
            if d <= calc:
                continue
            if d == dates[-1]:
                if sd > 0:
                    cfs.append((d, sd))
                cfs.append((d, par))
            else:
                cfs.append((d, sd))
        dirty = _ytm_dirty(calc, cfs, y)
        formula = "YTM (ефективна ставка)"

    clean = round(dirty - ai, 2)
    return round(dirty, 2), round(ai, 2), clean, ccy, formula

def secondary_yield_from_price(calc_date: str, isin: str, price_dirty: float, df: pd.DataFrame):
    """
    Інверсія вторинки:
      - дисконтні/останній купон → SIM інверсія,
      - купонні → YTM solve.
    """
    row = _get_bond_row(df, isin)
    calc = pd.to_datetime(calc_date).normalize()
    par, y_coup, k, maturity = float(row["Par_value"]), float(row["Coupon_rate"]), int(row["Coupon_per_year"]), row["Date_maturity"]
    dates = _coupon_dates_182_from_maturity(maturity, calc)
    sd = _semi_coupon_amount(par, y_coup, k)

    remain = sum(1 for d in dates if d > calc)
    if sd == 0.0 or remain <= 1:
        days = (maturity - calc).days
        t = max(0.0, days / DAY_COUNT)
        y = ((par + (sd if sd > 0 else 0.0)) / price_dirty - 1.0) / t if t > 0 else 0.0
        return {"Currency": row["Currency"], "Yield_percent": round(y * 100.0, 2), "Formula": "SIM (інверсія)"}
    else:
        # CF для YTM
        cfs = []
        for d in dates:
            if d <= calc:
                continue
            if d == dates[-1]:
                if sd > 0:
                    cfs.append((d, sd))
                cfs.append((d, par))
            else:
                cfs.append((d, sd))
        def f(y):
            return sum(amt/((1+y)**(((d-calc).days)/DAY_COUNT)) for d, amt in cfs) - price_dirty
        y = _solve_bisect(f, 1e-10, 2.0)
        return {"Currency": row["Currency"], "Yield_percent": round(y * 100.0, 2), "Formula": "YTM solve"}
