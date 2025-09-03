import pandas as pd
import numpy as np
from datetime import timedelta

# ---------- Невибагливий солвер кореня (без SciPy) ----------
def _bracket(func, lo=-0.99, hi=5.0, expand=1.5, max_tries=25):
    """Шукає інтервал [lo, hi] зі зміною знаку."""
    f_lo = func(lo)
    f_hi = func(hi)
    tries = 0
    while f_lo * f_hi > 0 and tries < max_tries:
        hi *= expand
        f_hi = func(hi)
        tries += 1
    return lo, hi, f_lo, f_hi

def _bisect(func, lo, hi, f_lo=None, f_hi=None, tol=1e-12, maxiter=200):
    """Бісекція (гарантовано збіжна при зміні знаку)."""
    if f_lo is None: f_lo = func(lo)
    if f_hi is None: f_hi = func(hi)
    if f_lo == 0: return lo
    if f_hi == 0: return hi
    for _ in range(maxiter):
        mid = 0.5 * (lo + hi)
        f_mid = func(mid)
        if abs(f_mid) < tol or (hi - lo) < tol:
            return mid
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return 0.5 * (lo + hi)

def _solve_root(func, lo=-0.99, hi=5.0, tol=1e-12, maxiter=200):
    """Комбо: спершу бранкуємо інтервал, потім бісекція. Без SciPy."""
    lo, hi, f_lo, f_hi = _bracket(func, lo, hi)
    # якщо все ще немає зміни знаку — повернемо середину (fallback)
    if f_lo * f_hi > 0:
        return 0.5 * (lo + hi)
    return _bisect(func, lo, hi, f_lo, f_hi, tol, maxiter)

# ================== ХЕЛПЕРИ КУПОНІВ / ПАРАМЕТРІВ ==================

def _extract_coupon_dates_from_row(bond_row: pd.Series):
    dates = []
    for col in bond_row.index:
        if isinstance(col, str) and "Термін сплати" in col:
            val = bond_row[col]
            try:
                d = pd.to_datetime(val) if pd.notna(val) else None
            except Exception:
                d = None
            if pd.notna(d):
                dates.append(pd.to_datetime(d).normalize())
    dates = sorted(set(dates))
    return dates

def _fallback_coupon_dates(issue_date, maturity_date, freq: int):
    issue_date = pd.to_datetime(issue_date).normalize()
    maturity_date = pd.to_datetime(maturity_date).normalize()
    step_days = 182 if (freq is None or int(freq) == 2) else max(1, 365 // int(freq))
    dates = []
    d = maturity_date
    while d >= issue_date:
        dates.insert(0, d)
        d -= timedelta(days=step_days)
    return dates

def _full_coupon_schedule_and_params(df: pd.DataFrame, isin: str):
    row = df[df["ISIN"] == isin]
    if row.empty:
        raise ValueError("ISIN не знайдено у довіднику.")
    b = row.iloc[0]

    par = float(b["Par_value"])
    ccy = b["Currency"]
    issue = pd.to_datetime(b["Date_Issue"]).normalize()
    maturity = pd.to_datetime(b["Date_maturity"]).normalize()
    coupon_rate = float(b["Yield_nominal"]) / 100.0 if pd.notna(b["Yield_nominal"]) else 0.0
    freq = int(b["Coupon_per_year"]) if pd.notna(b["Coupon_per_year"]) else 0

    dates = _extract_coupon_dates_from_row(b)
    if len(dates) < 2:
        dates = _fallback_coupon_dates(issue, maturity, freq if freq else 2)
    if maturity not in dates:
        dates.append(maturity)
        dates = sorted(dates)
    return dates, par, ccy, coupon_rate, (freq if freq else 0), issue, maturity

def build_cashflow_schedule(df: pd.DataFrame, isin: str, from_date=None):
    dates, par, ccy, coupon_rate, freq, issue, maturity = _full_coupon_schedule_and_params(df, isin)
    coupon_amount = round(par * coupon_rate / max(1, freq), 2) if coupon_rate > 0 and freq > 0 else 0.0

    if from_date is not None:
        from_date = pd.to_datetime(from_date).normalize()
        dates = [d for d in dates if d >= from_date]

    rows = []
    if coupon_amount > 0:
        for d in dates:
            rows.append({"date": d, "amount": coupon_amount, "type": "Купон", "currency": ccy})
    rows.append({"date": maturity, "amount": par, "type": "Погашення номіналу", "currency": ccy})

    rows.sort(key=lambda r: (r["date"], 1 if r["type"] != "Купон" else 0))
    out = pd.DataFrame(rows).drop_duplicates(subset=["date","type","amount","currency"]).reset_index(drop=True)
    out["date"] = out["date"].dt.strftime("%d/%m/%Y")
    return out, coupon_rate, ccy

# ================== НКД ==================

def accrued_interest(calc_date, isin, df):
    calc_date = pd.to_datetime(calc_date).normalize()
    dates, par, ccy, coupon_rate, freq, issue, maturity = _full_coupon_schedule_and_params(df, isin)
    if coupon_rate <= 0 or freq <= 0:
        return 0.0
    SD = par * coupon_rate / freq
    future = [d for d in dates if d >= calc_date]
    if not future:
        return 0.0
    next_coupon = future[0]
    idx_next = dates.index(next_coupon)
    if idx_next == 0:
        approx_step = (dates[1] - dates[0]).days if len(dates) >= 2 else max(1, 365 // freq)
        prev_coupon = next_coupon - pd.Timedelta(days=approx_step)
    else:
        prev_coupon = dates[idx_next - 1]
    KDP0 = (next_coupon - prev_coupon).days
    if KDP0 <= 0:
        KDP0 = max(1, 365 // freq)
    elapsed = (calc_date - prev_coupon).days
    elapsed = min(max(elapsed, 0), KDP0)
    AI = round(SD * (elapsed / KDP0), 2)
    return AI

# ================== ВТОРИННИЙ РИНОК: ЦІНА З ДОХІДНОСТІ ==================

def secondary_price_from_yield(calc_date, isin, yield_percent, df):
    """
    Правила:
      • Дисконтна → SIM: Price = Nom / (1 + y * Days/365)
      • Купонна:
          - якщо до погашення ≤182 днів або лишився лише останній купон → SIM:
              Price = (Nom + SD) / (1 + y * Days/365)
          - інакше → дисконтування по днях:
              Price = Σ SD/(1+y)^(days/365) + Nom/(1+y)^(days/365)
    Повертає: dirty, AI, clean, currency, formula_name
    """
    calc_date = pd.to_datetime(calc_date).normalize()
    dates, par, ccy, cr, freq, issue, maturity = _full_coupon_schedule_and_params(df, isin)
    y = float(yield_percent) / 100.0
    if calc_date >= maturity:
        return 0.0, 0.0, 0.0, ccy, "—"

    days_to_mty = (maturity - calc_date).days
    is_coupon = (cr > 0 and freq > 0)
    SD = par * cr / freq if is_coupon else 0.0

    if not is_coupon:
        price = par / (1 + y * (days_to_mty / 365.0))
        return round(price,2), 0.0, round(price,2), ccy, "SIM"

    future = [d for d in dates if d >= calc_date]
    short_or_last = (days_to_mty <= 182) or (len(future) == 1 and future[0] == maturity)
    if short_or_last:
        price = (par + SD) / (1 + y * (days_to_mty / 365.0))
        ai = accrued_interest(calc_date, isin, df)
        return round(price,2), ai, round(price-ai,2), ccy, "SIM"

    total = 0.0
    for d in future:
        t = (d - calc_date).days / 365.0
        total += SD / (1 + y) ** t
    total += par / (1 + y) ** (days_to_mty / 365.0)
    price = round(total, 2)
    ai = accrued_interest(calc_date, isin, df)
    return price, ai, round(price-ai,2), ccy, "YTM"

# ================== ПЕРВИННИЙ РИНОК: ЦІНА З ДОХІДНОСТІ ==================

def primary_price_from_yield_minfin(calc_date, isin, yield_percent, df):
    """
    Мінфін (купонна) або SIM (дисконтна).
    Формула Мінфіну (з показником):
      DF = (1 + y/freq) ** (Days / KDP0),
      де KDP0 — довжина поточного періоду (prev→next).
    Dirty = Σ SD/DF_i + Nom/DF_last; AI — окремо; Clean = Dirty - AI.
    """
    calc_date = pd.to_datetime(calc_date).normalize()
    dates, par, ccy, cr, freq, issue, maturity = _full_coupon_schedule_and_params(df, isin)
    y = float(yield_percent) / 100.0
    if calc_date >= maturity:
        return 0.0, 0.0, 0.0, ccy, "—"

    is_coupon = (cr > 0 and freq > 0)
    if not is_coupon:
        days = (maturity - calc_date).days
        price = par / (1 + y * (days / 365.0))
        return round(price,2), 0.0, round(price,2), ccy, "SIM"

    SD = par * cr / freq
    future = [d for d in dates if d >= calc_date]
    if not future:
        return 0.0, 0.0, 0.0, ccy, "MinFin"

    next_c = future[0]
    idx = dates.index(next_c)
    if idx == 0:
        approx_step = (dates[1] - dates[0]).days if len(dates) >= 2 else max(1, 365 // freq)
        prev_c = next_c - pd.Timedelta(days=approx_step)
    else:
        prev_c = dates[idx - 1]
    KDP0 = (next_c - prev_c).days
    if KDP0 <= 0:
        KDP0 = max(1, 365 // freq)

    base = 1.0 + y / freq
    dirty = 0.0
    for d in future:
        DD = (d - calc_date).days
        DF = base ** (DD / KDP0)
        dirty += SD / DF
        if d == pd.to_datetime(maturity):
            dirty += par / DF

    dirty = round(dirty, 2)
    ai = accrued_interest(calc_date, isin, df)
    clean = round(dirty - ai, 2)
    return dirty, ai, clean, ccy, "MinFin"

# ================== З ЦІНИ → ДОХІДНОСТІ ==================

def yields_from_price(calc_date, isin, price_dirty, df):
    """
    Рахує:
      • Вторинний ринок: SIM або YTM (в залежності від правил)
      • Первинний ринок: Мінфін або SIM
    Повертає словник із двома доходностями (округлено до 2 знаків).
    """
    calc_date = pd.to_datetime(calc_date).normalize()
    dates, par, ccy, cr, freq, issue, maturity = _full_coupon_schedule_and_params(df, isin)
    if calc_date >= maturity:
        raise ValueError("Дата розрахунку ≥ дати погашення.")
    days_to_mty = (maturity - calc_date).days
    is_coupon = (cr > 0 and freq > 0)
    SD = par * cr / freq if is_coupon else 0.0

    # ДИСКОНТНА: обидва ринки = SIM
    if not is_coupon:
        y_sim = (par / price_dirty - 1.0) * (365.0 / days_to_mty) * 100.0
        y_sim = round(y_sim, 2)
        return {
            "Secondary_yield": y_sim, "Secondary_formula": "SIM",
            "Primary_yield": y_sim,   "Primary_formula": "SIM",
            "Currency": ccy
        }

    # майбутні купони
    future = [d for d in dates if d >= calc_date]
    short_or_last = (days_to_mty <= 182) or (len(future) == 1 and future[0] == maturity)

    # Secondary: якщо короткий період → SIM, інакше YTM (NPV=0)
    if short_or_last:
        y_sec = ((par + SD) / price_dirty - 1.0) * (365.0 / days_to_mty) * 100.0
        sec_y, sec_f = round(y_sec, 2), "SIM"
    else:
        def pv_diff_y(y):
            y = max(y, -0.9999)
            total = 0.0
            for d in future:
                t = (d - calc_date).days / 365.0
                total += SD / (1.0 + y) ** t
            total += par / (1.0 + y) ** (days_to_mty / 365.0)
            return total - price_dirty

        lo, hi = -0.99, 5.0
        for _ in range(20):
            if pv_diff_y(lo) * pv_diff_y(hi) < 0:
                break
            hi *= 1.5
        try:
            y_root = _solve_root(pv_diff_y, lo, hi, maxiter=200, xtol=1e-12)
            sec_y, sec_f = round(y_root * 100.0, 2), "YTM"
        except Exception:
            y_f = ((par + SD) / price_dirty - 1.0) * (365.0 / days_to_mty) * 100.0
            sec_y, sec_f = round(y_f, 2), "SIM (fallback)"

    # Primary (Мінфін): DF = (1 + y/freq) ** (DD/KDP0)
    next_c = future[0]
    idx = dates.index(next_c)
    if idx == 0:
        approx_step = (dates[1] - dates[0]).days if len(dates) >= 2 else max(1, 365 // freq)
        prev_c = next_c - pd.Timedelta(days=approx_step)
    else:
        prev_c = dates[idx - 1]
    KDP0 = (next_c - prev_c).days
    if KDP0 <= 0:
        KDP0 = max(1, 365 // freq)

    def pv_diff_minfin(y):
        base = 1.0 + y / freq
        total = 0.0
        for d in future:
            DD = (d - calc_date).days
            DF = base ** (DD / KDP0)
            total += SD / DF
            if d == maturity:
                total += par / DF
        return total - price_dirty

    lo, hi = -0.99, 5.0
    for _ in range(20):
        if pv_diff_minfin(lo) * pv_diff_minfin(hi) < 0:
            break
        hi *= 1.5
    try:
        y_root_mf = _solve_root(pv_diff_minfin, lo, hi, maxiter=200, xtol=1e-12)
        prim_y, prim_f = round(y_root_mf * 100.0, 2), "MinFin"
    except Exception:
        y_f = ((par + SD) / price_dirty - 1.0) * (365.0 / days_to_mty) * 100.0
        prim_y, prim_f = round(y_f, 2), "SIM (fallback)"

    return {
        "Secondary_yield": sec_y, "Secondary_formula": sec_f,
        "Primary_yield": prim_y, "Primary_formula": prim_f,
        "Currency": ccy
    }

# ================== P&L: КУПИВ → ПРОДАВ ==================

def coupons_between(df, isin, start_date, end_date):
    start_date = pd.to_datetime(start_date).normalize()
    end_date = pd.to_datetime(end_date).normalize()
    dates, par, ccy, cr, freq, issue, maturity = _full_coupon_schedule_and_params(df, isin)
    if cr <= 0 or freq <= 0:
        return [], ccy
    SD = round(par * cr / freq, 2)
    out = []
    for d in dates:
        if start_date < d <= end_date:
            out.append((d.strftime("%d/%m/%Y"), SD))
    return out, ccy

def trade_outcome(isin, buy_date, buy_yield_percent, sell_date, sell_yield_percent, df):
    buy_date = pd.to_datetime(buy_date).normalize()
    sell_date = pd.to_datetime(sell_date).normalize()
    if sell_date <= buy_date:
        raise ValueError("Дата продажу має бути пізніше дати покупки.")

    buy_dirty, _, _, ccy, _ = secondary_price_from_yield(buy_date, isin, buy_yield_percent, df)
    sell_dirty, _, _, _, _ = secondary_price_from_yield(sell_date, isin, sell_yield_percent, df)

    cps, _ = coupons_between(df, isin, buy_date, sell_date)
    coupons_total = round(sum(a for _, a in cps), 2)

    profit_abs = round(sell_dirty + coupons_total - buy_dirty, 2)
    days_held = (sell_date - buy_date).days
    profit_ann_pct = round((profit_abs / buy_dirty) * (365.0 / days_held) * 100.0, 2) if days_held > 0 else None

    return {
        "ISIN": isin,
        "Currency": ccy,
        "Buy": {"date": buy_date.strftime("%Y-%m-%d"), "yield_percent": float(buy_yield_percent), "price_dirty": buy_dirty},
        "Sell": {"date": sell_date.strftime("%Y-%m-%d"), "yield_percent": float(sell_yield_percent), "price_dirty": sell_dirty},
        "Coupons_received": cps,
        "Coupons_total": coupons_total,
        "Profit_abs": profit_abs,
        "Profit_ann_pct": profit_ann_pct,
        "Days_held": days_held
    }
