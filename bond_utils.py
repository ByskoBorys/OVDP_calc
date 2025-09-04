import pandas as pd
import numpy as np

DAY_COUNT = 365.0

def _norm_pct_scalar(v) -> float:
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
    cpy = row.get("Coupon_per_year", 2)
    try: cpy = int(cpy) if pd.notna(cpy) else 2
    except: cpy = 2
    row["Coupon_per_year"] = max(0, cpy)

    # купон: сначала берём Coupon_rate (ожидаем десятичную долю из data_loader),
    # если его нет/ноль — попробуем Yield_nominal как прокси
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

def _generate_coupon_dates(issue, maturity, freq):
    if not freq or freq <= 0:
        return [maturity]
    months = int(round(12/freq))
    dates = [maturity]
    d = maturity
    for _ in range(60):
        d = d - pd.DateOffset(months=months)
        dates.append(d.normalize())
        if d <= issue - pd.DateOffset(days=1):
            break
    dates = sorted(set([x for x in dates if x >= issue]))
    if dates[-1] != maturity:
        dates.append(maturity)
        dates = sorted(set(dates))
    return dates

def _coupon_amount(par, coupon_rate, freq):
    return 0.0 if not freq or coupon_rate == 0 else par * coupon_rate / float(freq)

def _accrued_interest(calc_date, last_coupon, next_coupon, coupon_amt):
    if coupon_amt == 0.0: return 0.0
    days_since = (calc_date - last_coupon).days
    days_in = (next_coupon - last_coupon).days
    return 0.0 if days_in <= 0 else coupon_amt * (days_since / days_in)

def _future_cashflows(calc_date, coupon_dates, coupon_amt, par):
    cfs = []
    for d in coupon_dates:
        if d <= calc_date: continue
        amt = coupon_amt + (par if d == coupon_dates[-1] else 0.0)
        cfs.append((d, amt))
    return cfs

def build_cashflow_schedule(df, isin, from_date):
    row = _get_bond_row(df, isin)
    issue, maturity = row["Date_Issue"], row["Date_maturity"]
    freq, par, ccy = int(row["Coupon_per_year"]), float(row["Par_value"]), row["Currency"]
    coupon_rate = float(row["Coupon_rate"])
    coupons = _generate_coupon_dates(issue, maturity, freq)
    from_dt = pd.to_datetime(from_date).normalize()
    coupon_amt = _coupon_amount(par, coupon_rate, freq)
    cfs = _future_cashflows(from_dt, coupons, coupon_amt, par)
    sched = pd.DataFrame([(d.strftime("%Y-%m-%d"), round(a, 8)) for d,a in cfs], columns=["Дата","Сума"])
    return sched, coupon_rate, ccy

def _sim_price(calc, red_date, red_amt, y):
    days = (red_date - calc).days
    return red_amt / (1.0 + y * (days/365.0))

def _ytm_dirty(calc, cfs, y):
    dirty = 0.0
    for d, amt in cfs:
        t = (d - calc).days / 365.0
        dirty += amt / ((1.0 + y) ** t)
    return dirty

def _minfin_dirty(calc, cfs, y):
    dirty = 0.0
    for d, amt in cfs:
        t = (d - calc).days / 365.0
        dirty += amt / (1.0 + y * t)
    return dirty

def secondary_price_from_yield(calc_date, isin, y_pct, df):
    row = _get_bond_row(df, isin)
    calc = pd.to_datetime(calc_date).normalize()
    par, freq, cr, ccy = float(row["Par_value"]), int(row["Coupon_per_year"]), float(row["Coupon_rate"]), row["Currency"]
    maturity, issue = row["Date_maturity"], row["Date_Issue"]
    coupons = _generate_coupon_dates(issue, maturity, freq)
    coup_amt = _coupon_amount(par, cr, freq)
    last_c = max([d for d in coupons if d <= calc] or [issue])
    next_c = min([d for d in coupons if d > calc] or [maturity])
    y = float(y_pct)/100.0

    remain = sum(1 for d in coupons if d > calc)
    if coup_amt == 0.0 or remain <= 1:
        dirty = _sim_price(calc, maturity, par + (coup_amt if coup_amt>0 else 0.0), y)
        formula = "SIM (дисконт/останній купон)"
    else:
        cfs = _future_cashflows(calc, coupons, coup_amt, par)
        dirty = _ytm_dirty(calc, cfs, y)
        formula = "YTM (ефективна ставка)"

    ai = _accrued_interest(calc, last_c, next_c, coup_amt)
    clean = dirty - ai
    return round(dirty,2), round(ai,2), round(clean,2), ccy, formula

def primary_price_from_yield_minfin(calc_date, isin, y_pct, df):
    row = _get_bond_row(df, isin)
    calc = pd.to_datetime(calc_date).normalize()
    par, freq, cr, ccy = float(row["Par_value"]), int(row["Coupon_per_year"]), float(row["Coupon_rate"]), row["Currency"]
    maturity, issue = row["Date_maturity"], row["Date_Issue"]
    coupons = _generate_coupon_dates(issue, maturity, freq)
    coup_amt = _coupon_amount(par, cr, freq)
    last_c = max([d for d in coupons if d <= calc] or [issue])
    next_c = min([d for d in coupons if d > calc] or [maturity])
    y = float(y_pct)/100.0

    if coup_amt == 0.0:
        dirty = _sim_price(calc, maturity, par, y)
        formula = "SIM (дисконт, первинний)"
    else:
        cfs = _future_cashflows(calc, coupons, coup_amt, par)
        dirty = _minfin_dirty(calc, cfs, y)
        formula = "Мінфін (simple discount)"

    ai = _accrued_interest(calc, last_c, next_c, coup_amt)
    clean = dirty - ai
    return round(dirty,2), round(ai,2), round(clean,2), ccy, formula

def _solve_bisect(func, lo, hi, tol=1e-8, it=200):
    f_lo, f_hi = func(lo), func(hi)
    if f_lo == 0: return lo
    if f_hi == 0: return hi
    if f_lo * f_hi > 0:
        for _ in range(30):
            hi *= 1.5
            f_hi = func(hi)
            if f_lo * f_hi <= 0: break
    for _ in range(it):
        mid = 0.5*(lo+hi); f_mid = func(mid)
        if abs(f_mid) < tol: return mid
        if f_lo * f_mid <= 0: hi, f_hi = mid, f_mid
        else: lo, f_lo = mid, f_mid
    return 0.5*(lo+hi)

def yields_from_price(calc_date, isin, price_dirty, df):
    row = _get_bond_row(df, isin)
    calc = pd.to_datetime(calc_date).normalize()
    par, freq, cr, ccy = float(row["Par_value"]), int(row["Coupon_per_year"]), float(row["Coupon_rate"]), row["Currency"]
    maturity, issue = row["Date_maturity"], row["Date_Issue"]
    coupons = _generate_coupon_dates(issue, maturity, freq)
    coup_amt = _coupon_amount(par, cr, freq)
    last_c = max([d for d in coupons if d <= calc] or [issue])
    next_c = min([d for d in coupons if d > calc] or [maturity])

    remain = sum(1 for d in coupons if d > calc)
    if coup_amt == 0.0 or remain <= 1:
        days = (maturity - calc).days
        y_sec = ( (par + (coup_amt if coup_amt>0 else 0.0)) / price_dirty - 1.0 ) * (365.0 / days)
        sec_formula = "SIM (інверсія)"
    else:
        cfs = _future_cashflows(calc, coupons, coup_amt, par)
        f = lambda y: _ytm_dirty(calc, cfs, y) - price_dirty
        y_sec = _solve_bisect(f, 1e-6, 2.0)
        sec_formula = "YTM solve"

    if coup_amt == 0.0:
        days = (maturity - calc).days
        y_pri = (par / price_dirty - 1.0) * (365.0 / days)
        pri_formula = "SIM (інверсія, первинний)"
    else:
        cfs = _future_cashflows(calc, coupons, coup_amt, par)
        f2 = lambda y: _minfin_dirty(calc, cfs, y) - price_dirty
        y_pri = _solve_bisect(f2, 1e-6, 5.0)
        pri_formula = "Мінфін solve (simple discount)"

    ai = _accrued_interest(calc, last_c, next_c, coup_amt)
    return {
        "Currency": ccy,
        "Secondary_yield": round(y_sec*100.0, 2),
        "Secondary_formula": sec_formula,
        "Primary_yield": round(y_pri*100.0, 2),
        "Primary_formula": pri_formula,
        "AI_info": round(ai, 2),
    }

def trade_outcome(isin, buy_date, buy_yield_percent, sell_date, sell_yield_percent, df):
    ccy = _get_bond_row(df, isin)["Currency"]
    buy_dirty, *_ = secondary_price_from_yield(buy_date, isin, buy_yield_percent, df)
    sell_dirty, *_ = secondary_price_from_yield(sell_date, isin, sell_yield_percent, df)

    sched, _, _ = build_cashflow_schedule(df, isin, from_date=buy_date)
    sched_dt = [(pd.to_datetime(d), float(a)) for d,a in zip(sched["Дата"], sched["Сума"])]
    bdt, sdt = pd.to_datetime(buy_date).normalize(), pd.to_datetime(sell_date).normalize()
    coupons = [(d.strftime("%Y-%m-%d"), a) for d,a in sched_dt if bdt < d <= sdt]
    coupons_total = sum(a for _,a in coupons)

    profit_abs = sell_dirty - buy_dirty + coupons_total
    days_held = (sdt - bdt).days
    profit_ann_pct = round((profit_abs / buy_dirty) * (365.0 / days_held) * 100.0, 2) if days_held > 0 else None

    return {
        "ISIN": isin, "Currency": ccy,
        "Buy": {"date": str(bdt.date()), "yield_percent": float(buy_yield_percent), "price_dirty": round(buy_dirty,2)},
        "Sell": {"date": str(sdt.date()), "yield_percent": float(sell_yield_percent), "price_dirty": round(sell_dirty,2)},
        "Coupons_received": coupons, "Coupons_total": round(coupons_total,2),
        "Profit_abs": round(profit_abs,2), "Profit_ann_pct": profit_ann_pct, "Days_held": days_held
    }
