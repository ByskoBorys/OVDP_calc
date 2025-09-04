import pandas as pd
import numpy as np
from datetime import timedelta

DAY_COUNT = 365.0  # ACT/365

# ----------------------------- Utils -----------------------------
def _get_bond_row(df: pd.DataFrame, isin: str) -> pd.Series:
    if "ISIN" not in df.columns:
        raise KeyError("В довіднику немає колонки 'ISIN'.")
    row = df.loc[df["ISIN"] == isin]
    if row.empty:
        raise KeyError(f"ISIN не знайдено: {isin}")
    row = row.iloc[0].copy()

    # Normalize required fields with defaults
    row["Par_value"] = float(row.get("Par_value", 1000) if pd.notna(row.get("Par_value", np.nan)) else 1000)
    row["Coupon_per_year"] = int(row.get("Coupon_per_year", 0) if pd.notna(row.get("Coupon_per_year", np.nan)) else 0)
    row["Coupon_rate"] = float(row.get("Coupon_rate", 0.0) if pd.notna(row.get("Coupon_rate", np.nan)) else 0.0)
    row["Currency"] = str(row.get("Currency", "UAH")) if pd.notna(row.get("Currency", np.nan)) else "UAH"

    # Dates
    maturity = pd.to_datetime(row.get("Date_maturity", pd.NaT), errors="coerce")
    issue = pd.to_datetime(row.get("Date_Issue", pd.NaT), errors="coerce")
    if pd.isna(maturity):
        raise KeyError("У довіднику відсутня коректна Date_maturity.")
    if pd.isna(issue):
        # fallback: рік до погашення
        issue = maturity - pd.to_timedelta(365, unit="D")
    row["Date_maturity"] = maturity.normalize()
    row["Date_Issue"] = issue.normalize()
    return row

def _generate_coupon_dates(issue: pd.Timestamp, maturity: pd.Timestamp, freq: int) -> list:
    if freq is None or freq <= 0:
        return [maturity]
    # Генеруємо дати від погашення назад з кроком у місяцях 12/freq
    months = int(round(12/freq))
    dates = [maturity]
    d = maturity
    for _ in range(50):  # safety
        d = d - pd.DateOffset(months=months)
        dates.append(d.normalize())
        if d <= issue - pd.DateOffset(days=1):
            break
    dates = sorted(list(set(dates)))
    # Відфільтруємо дати раніше за issue + невеликий запас
    dates = [x for x in dates if x >= issue]
    if dates[-1] != maturity:
        dates.append(maturity)
        dates = sorted(list(set(dates)))
    return dates

def _coupon_amount(par: float, coupon_rate: float, freq: int) -> float:
    if freq is None or freq <= 0 or coupon_rate == 0:
        return 0.0
    return par * (coupon_rate) / float(freq)

def _accrued_interest(calc_date: pd.Timestamp, last_coupon: pd.Timestamp, next_coupon: pd.Timestamp, coupon_amt: float) -> float:
    if coupon_amt == 0.0:
        return 0.0
    days_since = (calc_date - last_coupon).days
    days_in_period = (next_coupon - last_coupon).days
    if days_in_period <= 0:
        return 0.0
    return coupon_amt * (days_since / days_in_period)

def _future_cashflows(calc_date: pd.Timestamp, coupon_dates: list, coupon_amt: float, par: float):
    cfs = []
    for d in coupon_dates:
        if d <= calc_date:
            continue
        amt = coupon_amt
        if d == coupon_dates[-1]:
            amt += par
        cfs.append((d, amt))
    return cfs

# --------------------------- Schedules ---------------------------
def build_cashflow_schedule(df: pd.DataFrame, isin: str, from_date: str):
    row = _get_bond_row(df, isin)
    issue = row["Date_Issue"]
    maturity = row["Date_maturity"]
    freq = int(row["Coupon_per_year"])
    par = float(row["Par_value"])
    ccy = row["Currency"]
    coupon_rate = float(row["Coupon_rate"])  # decimal, e.g., 0.16

    coupons = _generate_coupon_dates(issue, maturity, freq)
    from_dt = pd.to_datetime(from_date).normalize()
    coupon_amt = _coupon_amount(par, coupon_rate, freq)
    cfs = _future_cashflows(from_dt, coupons, coupon_amt, par)
    sched = pd.DataFrame([(d.strftime("%Y-%m-%d"), round(a, 8)) for d,a in cfs], columns=["Дата", "Сума"])
    return sched, coupon_rate, ccy

# --------------------------- Pricing -----------------------------
def _sim_price(calc_date: pd.Timestamp, redemption_date: pd.Timestamp, redemption_amt: float, y: float) -> float:
    days = (redemption_date - calc_date).days
    return redemption_amt / (1.0 + y * (days / DAY_COUNT))

def _ytm_dirty(calc_date: pd.Timestamp, cfs: list, y: float) -> float:
    # y is effective annual (decimal), discount continuously by (1+y)^{t}
    dirty = 0.0
    for d, amt in cfs:
        t = (d - calc_date).days / DAY_COUNT
        dirty += amt / ((1.0 + y) ** t)
    return dirty

def _minfin_dirty(calc_date: pd.Timestamp, cfs: list, y: float) -> float:
    # Simple interest discounting each cashflow: CF / (1 + y * t)
    dirty = 0.0
    for d, amt in cfs:
        t = (d - calc_date).days / DAY_COUNT
        dirty += amt / (1.0 + y * t)
    return dirty

def secondary_price_from_yield(calc_date: str, isin: str, yield_percent: float, df: pd.DataFrame):
    """
    Вторинний ринок:
      - Якщо купон 0 або залишився останній купон -> формула СІМ.
      - Інакше -> YTM (ефективна ставка, дисконтуємо (1+y)^t).
    Повертаємо: dirty, AI, clean, ccy, formula_label
    """
    row = _get_bond_row(df, isin)
    calc = pd.to_datetime(calc_date).normalize()
    par = float(row["Par_value"])
    freq = int(row["Coupon_per_year"])
    coupon_rate = float(row["Coupon_rate"])
    ccy = row["Currency"]
    maturity = row["Date_maturity"]
    issue = row["Date_Issue"]

    coupons = _generate_coupon_dates(issue, maturity, freq)
    coupon_amt = _coupon_amount(par, coupon_rate, freq)
    # last and next coupons for AI
    last_coupon = max([d for d in coupons if d <= calc] or [issue])
    next_coupon = min([d for d in coupons if d > calc] or [maturity])

    y = float(yield_percent) / 100.0
    # decide SIM or YTM
    remain_coupons = sum(1 for d in coupons if d > calc)
    if coupon_amt == 0.0 or remain_coupons <= 1:
        redemption = par + (coupon_amt if coupon_amt > 0 else 0.0)
        dirty = _sim_price(calc, maturity, redemption, y)
        formula = "SIM (дисконт/останній купон)"
    else:
        cfs = _future_cashflows(calc, coupons, coupon_amt, par)
        dirty = _ytm_dirty(calc, cfs, y)
        formula = "YTM (ефективна ставка)"

    ai = _accrued_interest(calc, last_coupon, next_coupon, coupon_amt)
    clean = dirty - ai
    return round(dirty, 2), round(ai, 2), round(clean, 2), ccy, formula

def primary_price_from_yield_minfin(calc_date: str, isin: str, yield_percent: float, df: pd.DataFrame):
    """
    Первинний ринок:
      - Дисконтні -> SIM
      - Купонні -> Мінфін: просте дисконтування кожного грошового потоку (1 + y * t)
    """
    row = _get_bond_row(df, isin)
    calc = pd.to_datetime(calc_date).normalize()
    par = float(row["Par_value"])
    freq = int(row["Coupon_per_year"])
    coupon_rate = float(row["Coupon_rate"])
    ccy = row["Currency"]
    maturity = row["Date_maturity"]
    issue = row["Date_Issue"]

    coupons = _generate_coupon_dates(issue, maturity, freq)
    coupon_amt = _coupon_amount(par, coupon_rate, freq)
    last_coupon = max([d for d in coupons if d <= calc] or [issue])
    next_coupon = min([d for d in coupons if d > calc] or [maturity])

    y = float(yield_percent) / 100.0
    if coupon_amt == 0.0:
        redemption = par
        dirty = _sim_price(calc, maturity, redemption, y)
        formula = "SIM (дисконт, первинний)"
    else:
        cfs = _future_cashflows(calc, coupons, coupon_amt, par)
        dirty = _minfin_dirty(calc, cfs, y)
        formula = "Мінфін (simple discount)"

    ai = _accrued_interest(calc, last_coupon, next_coupon, coupon_amt)
    clean = dirty - ai
    return round(dirty, 2), round(ai, 2), round(clean, 2), ccy, formula

# ------------------------ Yield from price -----------------------
def _solve_bisect(func, lo, hi, tol=1e-8, max_iter=200):
    f_lo = func(lo)
    f_hi = func(hi)
    if f_lo == 0:
        return lo
    if f_hi == 0:
        return hi
    if f_lo * f_hi > 0:
        # expand range
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
            hi = mid
            f_hi = f_mid
        else:
            lo = mid
            f_lo = f_mid
    return 0.5 * (lo + hi)

def yields_from_price(calc_date: str, isin: str, price_dirty: float, df: pd.DataFrame):
    """
    Повертає словник:
      - Currency
      - Secondary_yield, Secondary_formula
      - Primary_yield, Primary_formula
    """
    row = _get_bond_row(df, isin)
    calc = pd.to_datetime(calc_date).normalize()
    par = float(row["Par_value"])
    freq = int(row["Coupon_per_year"])
    coupon_rate = float(row["Coupon_rate"])
    ccy = row["Currency"]
    maturity = row["Date_maturity"]
    issue = row["Date_Issue"]

    coupons = _generate_coupon_dates(issue, maturity, freq)
    coupon_amt = _coupon_amount(par, coupon_rate, freq)
    last_coupon = max([d for d in coupons if d <= calc] or [issue])
    next_coupon = min([d for d in coupons if d > calc] or [maturity])
    # AI purely informational, not used in solving because price passed is dirty
    ai = _accrued_interest(calc, last_coupon, next_coupon, coupon_amt)

    # Secondary yield
    remain_coupons = sum(1 for d in coupons if d > calc)
    if coupon_amt == 0.0 or remain_coupons <= 1:
        # SIM inversion
        redemption = par + (coupon_amt if coupon_amt > 0 else 0.0)
        days = (maturity - calc).days
        y_sec = (redemption / price_dirty - 1.0) * (DAY_COUNT / days)
        sec_formula = "SIM (інверсія)"
    else:
        cfs = _future_cashflows(calc, coupons, coupon_amt, par)
        def f(y):
            return _ytm_dirty(calc, cfs, y) - price_dirty
        y_sec = _solve_bisect(f, 0.000001, 2.0)  # 0%..200% eff.
        sec_formula = "YTM solve"

    # Primary yield (MinFin)
    if coupon_amt == 0.0:
        redemption = par
        days = (maturity - calc).days
        y_pri = (redemption / price_dirty - 1.0) * (DAY_COUNT / days)
        pri_formula = "SIM (інверсія, первинний)"
    else:
        cfs = _future_cashflows(calc, coupons, coupon_amt, par)
        def f2(y):
            return _minfin_dirty(calc, cfs, y) - price_dirty
        y_pri = _solve_bisect(f2, 0.000001, 5.0)
        pri_formula = "Мінфін solve (simple discount)"

    return {
        "Currency": ccy,
        "Secondary_yield": round(y_sec * 100.0, 2),
        "Secondary_formula": sec_formula,
        "Primary_yield": round(y_pri * 100.0, 2),
        "Primary_formula": pri_formula,
        "AI_info": round(ai, 2),
    }

# --------------------------- Trade outcome -----------------------
def trade_outcome(isin: str, buy_date: str, buy_yield_percent: float,
                  sell_date: str, sell_yield_percent: float, df: pd.DataFrame):
    """
    Розрахунок P&L: використовує **вторинний ринок** для ціноутворення (dirty).
    """
    row = _get_bond_row(df, isin)
    ccy = row["Currency"]

    buy_dirty, _, _, _, _ = secondary_price_from_yield(buy_date, isin, buy_yield_percent, df)
    sell_dirty, _, _, _, _ = secondary_price_from_yield(sell_date, isin, sell_yield_percent, df)

    # Купони отримані між датами (ex-date нехай співпадає з датою купону)
    sched, coupon_rate, _ = build_cashflow_schedule(df, isin, from_date=buy_date)
    sched_dt = [(pd.to_datetime(d), float(a)) for d, a in zip(sched["Дата"], sched["Сума"])]
    bdt = pd.to_datetime(buy_date).normalize()
    sdt = pd.to_datetime(sell_date).normalize()
    coupons = [(d.strftime("%Y-%m-%d"), a) for d, a in sched_dt if bdt < d <= sdt]
    coupons_total = sum(a for _, a in coupons)

    profit_abs = sell_dirty - buy_dirty + coupons_total
    days_held = (sdt - bdt).days
    profit_ann_pct = round((profit_abs / buy_dirty) * (DAY_COUNT / days_held) * 100.0, 2) if days_held > 0 else None

    return {
        "ISIN": isin,
        "Currency": ccy,
        "Buy": {"date": str(pd.to_datetime(buy_date).date()), "yield_percent": float(buy_yield_percent), "price_dirty": round(buy_dirty,2)},
        "Sell": {"date": str(pd.to_datetime(sell_date).date()), "yield_percent": float(sell_yield_percent), "price_dirty": round(sell_dirty,2)},
        "Coupons_received": coupons,
        "Coupons_total": round(coupons_total,2),
        "Profit_abs": round(profit_abs,2),
        "Profit_ann_pct": profit_ann_pct,
        "Days_held": days_held
    }
