"""Bounded US scheduled equity sessions, cross-checked with broker state before orders.

Sources retrieved 2026-10-31:
https://www.nyse.com/trade/hours-calendars (2026-2028)
https://www.nasdaqtrader.com/trader.aspx?id=Calendar (2026)
Unscheduled closures and broker-specific availability require fresh NH evidence.
"""
from datetime import date,datetime,time,timedelta
from zoneinfo import ZoneInfo

NY=ZoneInfo('America/New_York')
SOURCE='NYSE published 2026–2028 holiday calendar; Nasdaq 2026 cross-check'
HOLIDAYS={
    2026:('01-01','01-19','02-16','04-03','05-25','06-19','07-03','09-07','11-26','12-25'),
    2027:('01-01','01-18','02-15','03-26','05-31','06-18','07-05','09-06','11-25','12-24'),
    2028:('01-17','02-21','04-14','05-29','06-19','07-04','09-04','11-23','12-25'),
}
EARLY={'2026-11-27','2026-12-24','2027-11-26','2028-07-03','2028-11-24'}


def session(day):
    value=date.fromisoformat(day) if isinstance(day,str) else day
    if not isinstance(value,date) or isinstance(value,datetime):
        raise ValueError('거래일 날짜를 확인하세요.')
    if value.year not in HOLIDAYS:
        raise ValueError('검증한 미국 거래 달력 범위(2026~2028)를 벗어났습니다. 달력 갱신이 필요합니다.')
    if value.weekday()>=5 or value.strftime('%m-%d') in HOLIDAYS[value.year]:
        return None
    early=value.isoformat() in EARLY
    return {'date':value.isoformat(),'open':datetime.combine(value,time(9,30),NY),
            'close':datetime.combine(value,time(13 if early else 16),NY),
            'after_close':datetime.combine(value,time(17 if early else 20),NY),
            'early_close':early,'source':SOURCE}


def latest_completed(now):
    if not isinstance(now,datetime) or now.tzinfo is None:
        raise ValueError('현재 시각의 시간대를 확인하세요.')
    local=now.astimezone(NY);day=local.date()
    for _ in range(15):
        info=session(day)
        if info and local>=info['close']+timedelta(minutes=10):
            return day.isoformat()
        day-=timedelta(days=1)
    raise ValueError('최근 완료 거래일을 확인할 수 없습니다.')
