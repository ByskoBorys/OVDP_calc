import pandas as pd
import numpy as np

DAY_COUNT = 365.0  # ACT/365

# ----------------------------- helpers -----------------------------

def _norm_pct_scalar(v) -> float:
    """Нормалізує значення у десятковий відсоток (0.16)."""
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return 0.0
        if isinstance(v, str):
            s = v.replace("%", "").replace(",", ".").strip()
            x = float(s)
        else:
            x = float(v)
        return x / 100.0 if x > 1.0 else x
    except Exception:
        return 0.0

def _get_bond_row(df: pd.DataFrame, isin: str) -> pd.Series:
    row = df.loc[df["ISIN"] == isin]
    if row.empty:
        raise KeyError(f"ISIN не знайдено: {isin}")
    row = row.iloc[0].copy()

    row["Par_value"] = float(row.get("Par_value", 1000) if pd.notna(row.get("Par_value", np.nan)) else 1000)

    # частота купонів
    cpy = row.get("Coupon_per_year", 2)
    try:
        cpy = int(cpy) if pd.notna(cpy) else 2
    except Exception:
        cpy = 2
    row["Coupon_per_year"] = max(0, cpy)

    # купонна ставка (десяткова): спочатку Coupon_rate, якщо немає — беремо Yield_nominal
    coup = row.get("Coupon_rate", np.nan)
    if pd.isna(coup) or float(coup) == 0.0:
        coup = row.get("Yield_nominal", np.nan)
    row["Coupon_rate"] = _norm_pct_scalar(coup) if isinstance(coup, str) else (float(coup) if pd.notna(coup) else 0.0)

    row["Currency"] = str(row.get("Currency", "UAH")) if pd.notna(row.get("Currency", np.nan)) else "UAH"

    maturity = pd.to_datetime(row.get("Date_maturity", pd.NaT), errors="coerce")
    issue = pd.to_datetime(row.get("Date_Issue", pd.NaT), errors="coerce")
    if pd.isna(maturity):
        raise KeyError("В довіднику відсутня коректна Date_maturity.")
    if pd.isna(issue):
        issue = maturity - pd.to_timedelta(365, unit="D")

    row["Date_maturity"] = maturity.normalize()
    row["Date_Issue"] = issue.normalize()
    return row

# --- генерація купонних дат «кроком у днях» ---

def _step_days_for_freq(freq: int) -> int:
    """
    Фіксуємо крок у днях для головних частот:
      1 → 365, 2 → 182, 4 → 91, 12 → 30.
    Для інших — округлюємо 365/freq.
    """
    if freq <= 0:
        return 365
    preset = {1: 365, 2: 182, 4: 91, 12: 30}
    return preset.get(freq, int(round(365 / freq)))

def _coupon_dates_by_days(maturity: pd.Timestamp, freq: int, around_date: pd.Timestamp):
    """
    Будуємо ряд купонних дат, відраховуючи назад від погашення фіксованим кроком у днях,
    доки не пройдемо дату розрахунку. Повертаємо відсортований список дат.
    """
    step = _step_days_for_freq(freq)
    d = maturity.normalize()
    dates = [d]
    # рухаємось назад, поки не перейдемо дату розрахунку ще на один крок
    while True:
        d = d - pd.Timedelta(days=step)
        dates.append(d)
        if d <= around_date - pd.Timedelta(days=step):
            break
    dates = sorted(set(dates))
    return dates

def _coupon_amount(par: float, coupon_rate: float, freq: int) -> float:
    if freq is None or freq <= 0 or coupon_rate == 0:
        return 0.0
    return par * coupon_rate / float(freq)

def _accrued_interest(calc_date: pd.Timestamp, last_coupon: pd.Timestamp, next_coupon: pd.Timestamp, coupon_amt: float) -> float:
    if coupon_amt == 0.0:
        return 0.0
    days_since = (calc_date - last_coupon).days
    days_in = (next_coupon - last_coupon).days
    return 0.0 if days_in <= 0 else coupon_amt * (days_since / days_in)

def _future_cashflows(calc_date: pd.Timestamp, coupon_dates: list, coupon_amt: float, par: float):
    cfs = []
    for d in coupon_dates:
        if d <= calc_date:
            continue
        amt = coupon_amt + (par if d == coupon_dates[-1] else 0.0)
        cfs.append((d, amt))
    return cfs

# --------------------------- SCHEDULE -----------------------------

def build_cashflow_schedule(df: pd.DataFrame, isin: str, from_date: str):
    """Графік від дати розрахунку: maturity, maturity−крок, ... тільки майбутні потоки."""
    row = _get_bond_row(df, isin)
    maturity = row["Date_maturity"]
    freq = int(row["Coupon_per_year"]) or 2
    par = float(row["Par_value"])
    ccy = row["Currency"]
    coupon_rate = float(row["Coupon_rate"])

    calc = pd.to_datetime(from_date).normalize()
    coupons = _coupon_dates_by_days(maturity, freq, calc)
    coupon_amt = _coupon_amount(par, coupon_rate, freq)
    cfs = _future_cashflows(calc, coupons, coupon_amt, par)

    sched = pd.DataFrame([(d.strftime("%Y-%m-%d"), round(a, 8)) for d, a in cfs], columns=["Дата", "Сума"])
    return sched, coupon_rate, ccy

# ---------------------------- PRICING -----------------------------

def _sim_price(calc: pd.Timestamp, red_date: pd.Timestamp, red_amt: float, y: float) -> float:
    days = (red_date - calc).days
    return red_amt / (1.0 + y * (days / DAY_COUNT))

def _ytm_dirty(calc: pd.Timestamp, cfs: list, y: float) -> float:
    dirty = 0.0
    for d, amt in cfs:
        t = (d - calc).days / DAY_COUNT
        dirty += amt / ((1.0 + y) ** t)
    return dirty

def _minfin_dirty(calc: pd.Timestamp, cfs: list, y: float) -> float:
    dirty = 0.0
    for d, amt in cfs:
        t = (d - calc).days / DAY_COUNT
        dirty += amt / (1.0 + y * t)
    return dirty

def _last_next_coupon(calc: pd.Timestamp, coupons: list, freq: int):
    """Знаходимо купон до/після дати розрахунку, з урахуванням нашої часової сітки."""
    step = _step_days_for_freq(freq)
    future = sorted([d for d in coupons if d > calc])
    next_c = future[0] if future else coupons[-1]
    # last: або найближча <= calc, або next - step
    past = sorted([d for d in coupons if d <= calc])
    last_c = past[-1] if past else next_c - pd.Timedelta(days=step)
    return last_c, next_c

def secondary_price_from_yield(calc_date: str, isin: str, yield_percent: float, df: pd.DataFrame):
    """
    Вторинний ринок:
      - якщо купон = 0 або залишився один платіж → SIM,
      - інакше → YTM (ефективна ставка, дисконтуємо (1+y)^t).
    """
    row = _get_bond_row(df, isin)
    calc = pd.to_datetime(calc_date).normalize()
    par = float(row["Par_value"])
    freq = int(row["Coupon_per_year"]) or 2
    coupon_rate = float(row["Coupon_rate"])
    ccy = row["Currency"]
    maturity = row["Date_maturity"]

    coupons = _coupon_dates_by_days(maturity, freq, calc)
    coupon_amt = _coupon_amount(par, coupon_rate, freq)
    last_c, next_c = _last_next_coupon(calc, coupons, freq)

    y = float(yield_percent) / 100.0
    remain = sum(1 for d in coupons if d > calc)
    if coupon_amt == 0.0 or remain <= 1:
        dirty = _sim_price(calc, maturity, par + (coupon_amt if coupon_amt > 0 else 0.0), y)
        formula = "SIM (дисконт/останній купон)"
    else:
        cfs = _future_cashflows(calc, coupons, coupon_amt, par)
        dirty = _ytm_dirty(calc, cfs, y)
        formula = "YTM (ефективна ставка)"

    ai = _accrued_interest(calc, last_c, next_c, coupon_amt)
    clean = dirty - ai
    return round(dirty, 2), round(ai, 2), round(clean, 2), ccy, formula

def primary_price_from_yield_minfin(calc_date: str, isin: str, yield_percent: float, df: pd.DataFrame):
    """
    Первинний ринок:
      - дисконтні → SIM,
      - купонні → просте дисконтування кожного потоку (Мінфін): CF / (1 + y*t).
    """
    row = _get_bond_row(df, isin)
    calc = pd.to_datetime(calc_date).normalize()
    par = float(row["Par_value"])
    freq = int(row["Coupon_per_year"]) or 2
    coupon_rate = float(row["Coupon_rate"])
    ccy = row["Currency"]
    maturity = row["Date_maturity"]

    coupons = _coupon_dates_by_days(maturity, freq, calc)
    coupon_amt = _coupon_amount(par, coupon_rate, freq)
    last_c, next_c = _last_next_coupon(calc, coupons, freq)

    y = float(yield_percent) / 100.0
    if coupon_amt == 0.0:
        dirty = _sim_price(calc, maturity, par, y)
        formula = "SIM (дисконт, первинний)"
    else:
        cfs = _future_cashflows(calc, coupons, coupon_amt, par)
        dirty = _minfin_dirty(calc, cfs, y)
        formula = "Мінфін (simple discount)"

    ai = _accrued_interest(calc, last_c, next_c, coupon_amt)
    clean = dirty - ai
    return round(dirty, 2), round(ai, 2), round(clean, 2), ccy, formula

# ------------------------- Yield from price -----------------------

def _solve_bisect(func, lo, hi, tol=1e-8, max_iter=200):
    f_lo, f_hi = func(lo), func(hi)
    if f_lo == 0: return lo
    if f_hi == 0: return hi
    if f_lo * f_hi > 0:
        for _ in range(30):
            hi *= 1.5
            f_hi = func(hi)
            if f_lo * f_hi <= 0:
                break
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        f_mid = func(mid)
        if abs(f_mid) < tol:
            return mid
        if f_lo * f_mid <= 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return 0.5 * (lo + hi)

def yields_from_price(calc_date: str, isin: str, price_dirty: float, df: pd.DataFrame):
    row = _get_bond_row(df, isin)
    calc = pd.to_datetime(calc_date).normalize()
    par = float(row["Par_value"])
    freq = int(row["Coupon_per_year"]) or 2
    coupon_rate = float(row["Coupon_rate"])
    ccy = row["Currency"]
    maturity = row["Date_maturity"]

    coupons = _coupon_dates_by_days(maturity, freq, calc)
    coupon_amt = _coupon_amount(par, coupon_rate, freq)
    last_c, next_c = _last_next_coupon(calc, coupons, freq)

    # Secondary
    remain = sum(1 for d in coupons if d > calc)
    if coupon_amt == 0.0 or remain <= 1:
        days = (maturity - calc).days
        y_sec = ((par + (coupon_amt if coupon_amt > 0 else 0.0)) / price_dirty - 1.0) * (DAY_COUNT / days)
        sec_formula = "SIM (інверсія)"
    else:
        cfs = _future_cashflows(calc, coupons, coupon_amt, par)
        def f(y): return _ytm_dirty(calc, cfs, y) - price_dirty
        y_sec = _solve_bisect(f, 1e-6, 2.0)
        sec_formula = "YTM solve"

    # Primary (MinFin)
    if coupon_amt == 0.0:
        days = (maturity - calc).days
        y_pri = (par / price_dirty - 1.0) * (DAY_COUNT / days)
        pri_formula = "SIM (інверсія, первинний)"
    else:
        cfs = _future_cashflows(calc, coupons, coupon_amt, par)
        def f2(y): return _minfin_dirty(calc, cfs, y) - price_dirty
        y_pri = _solve_bisect(f2, 1e-6, 5.0)
        pri_formula = "Мінфін solve (simple discount)"

    ai = _accrued_interest(calc, last_c, next_c, coupon_amt)

    return {
        "Currency": ccy,
        "Secondary_yield": round(y_sec * 100.0, 2),
        "Secondary_formula": sec_formula,
        "Primary_yield": round(y_pri * 100.0, 2),
        "Primary_formula": pri_formula,
        "AI_info": round(ai, 2),
    }

# ---------------------------- Trade P&L ---------------------------

def trade_outcome(isin: str, buy_date: str, buy_yield_percent: float,
                  sell_date: str, sell_yield_percent: float, df: pd.DataFrame):
    row = _get_bond_row(df, isin)
    ccy = row["Currency"]
    maturity = row["Date_maturity"]
    freq = int(row["Coupon_per_year"]) or 2
    par = float(row["Par_value"])
    coupon_rate = float(row["Coupon_rate"])

    buy_dirty, _, _, _, _ = secondary_price_from_yield(buy_date, isin, buy_yield_percent, df)
    sell_dirty, _, _, _, _ = secondary_price_from_yield(sell_date, isin, sell_yield_percent, df)

    # купони між датами володіння
    sched_buy_side = _coupon_dates_by_days(maturity, freq, pd.to_datetime(buy_date))
    coup_amt = _coupon_amount(par, coupon_rate, freq)
    bdt = pd.to_datetime(buy_date).normalize()
    sdt = pd.to_datetime(sell_date).normalize()
    coupons = [(d.strftime("%Y-%m-%d"), coup_amt + (par if d == sched_buy_side[-1] else 0.0))
               for d in sched_buy_side if bdt < d <= sdt]
    coupons_total = sum(a for _, a in coupons)

    profit_abs = sell_dirty - buy_dirty + coupons_total
    days_held = (sdt - bdt).days
    profit_ann_pct = round((profit_abs / buy_dirty) * (DAY_COUNT / days_held) * 100.0, 2) if days_held > 0 else None

    return {
        "ISIN": isin,
        "Currency": ccy,
        "Buy": {"date": str(bdt.date()), "yield_percent": float(buy_yield_percent), "price_dirty": round(buy_dirty, 2)},
        "Sell": {"date": str(sdt.date()), "yield_percent": float(sell_yield_percent), "price_dirty": round(sell_dirty, 2)},
        "Coupons_received": coupons,
        "Coupons_total": round(coupons_total, 2),
        "Profit_abs": round(profit_abs, 2),
        "Profit_ann_pct": profit_ann_pct,
        "Days_held": days_held,
    }
