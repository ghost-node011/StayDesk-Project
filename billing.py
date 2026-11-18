"""Money rules for the hostel, kept free of any database code so they can be tested alone.

All amounts are Decimal and rounded to whole rupees (half up), which is how
hostels in India usually bill.
"""
import calendar
import datetime as dt
from decimal import ROUND_HALF_UP, Decimal

DUE_DAY = 10                      # rent for a month is due on the 10th
NEW_ADMISSION_GRACE_DAYS = 7      # someone admitted after the 10th gets 7 days to pay
LATE_FEE_PER_DAY = Decimal("50")  # charged for each day past the due date while a balance remains
LATE_FEE_CAP = Decimal("500")     # per bill
ONE_RUPEE = Decimal("1")


def money(value):
    return Decimal(value).quantize(ONE_RUPEE, rounding=ROUND_HALF_UP)


def month_start(day):
    return day.replace(day=1)


def month_end(month):
    return month.replace(day=calendar.monthrange(month.year, month.month)[1])


def add_months(month, n):
    index = month.year * 12 + (month.month - 1) + n
    return dt.date(index // 12, index % 12 + 1, 1)


def prorated_rent(monthly_rent, month, admitted_on, checkout_on=None):
    """Rent for `month`, charged only for the days the resident actually stays.

    Returns (amount, days_billed, days_in_month). Both the admission day and the
    checkout day count as days stayed.
    """
    month = month_start(month)
    last = month_end(month)
    days_in_month = last.day
    start = max(month, admitted_on)
    end = min(last, checkout_on) if checkout_on else last
    if end < start:
        return Decimal("0"), 0, days_in_month
    days = (end - start).days + 1
    if days == days_in_month:
        return money(monthly_rent), days, days_in_month
    return money(Decimal(monthly_rent) * days / days_in_month), days, days_in_month


def due_date(month, admitted_on):
    """The 10th of the month, or a week after admission for someone who joined later."""
    regular = month_start(month).replace(day=DUE_DAY)
    if month_start(admitted_on) == month_start(month):
        return max(regular, admitted_on + dt.timedelta(days=NEW_ADMISSION_GRACE_DAYS))
    return regular


def late_fee(due, as_of, outstanding):
    """Late fee owed on a bill that still has `outstanding` money on `as_of`."""
    if outstanding <= 0 or as_of <= due:
        return Decimal("0")
    return min(LATE_FEE_CAP, LATE_FEE_PER_DAY * (as_of - due).days)


def settlement(deposit, dues, damages):
    """Deposit left to refund at checkout. Negative means the resident still owes money."""
    return money(Decimal(deposit) - Decimal(dues) - Decimal(damages))
