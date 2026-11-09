#!/usr/bin/env python3
"""DID 금리·유가 페이지용 데이터 수집.

GitHub Actions에서 실행되어 markets.json 을 만든다. 표준 라이브러리만 사용한다.
출처마다 따로 받아서, 실패한 항목은 직전 파일(prev)의 값을 그대로 유지한다.

  python3 fetch_markets.py <prev.json> <out.json>

출처
  - 브렌트유·WTI: Yahoo Finance 선물 근월물 (BZ=F, CL=F)
  - 미국 기준금리: 뉴욕 연준 EFFR API의 목표 범위
  - 미국 국채 10년: 미 재무부 Daily Treasury Par Yield Curve
  - 한국 기준금리·국고채 10년: 한국은행 ECOS (ECOS_API_KEY, 없으면 시험용 sample 키)

각 항목의 hist 는 추이 그래프용 [날짜, 값] 목록(오래된 순)이다.
"""
import csv
import datetime as dt
import io
import json
import os
import sys
import urllib.request

UA = 'Mozilla/5.0 (compatible; stech-did/1.0)'
KST = dt.timezone(dt.timedelta(hours=9))
ECOS_KEY = os.environ.get('ECOS_API_KEY') or 'sample'
# 시험용 sample 키는 한 번에 10건까지만 준다
ECOS_MAX = 10 if ECOS_KEY == 'sample' else 100


def get(url, timeout=30):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': '*/*'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8-sig')


def utc_date(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc)


def last_change(points):
    """[날짜, 값] 목록에서 마지막으로 값이 바뀐 지점."""
    for i in range(len(points) - 1, 0, -1):
        if points[i][1] != points[i - 1][1]:
            return {'date': points[i][0], 'delta': round(points[i][1] - points[i - 1][1], 4)}
    return None


# ── 유가 ──────────────────────────────────────────

def yahoo_chart(symbol, query):
    last = None
    for host in ('query1', 'query2'):
        try:
            url = 'https://%s.finance.yahoo.com/v8/finance/chart/%s?%s' % (
                host, urllib.request.quote(symbol), query)
            return json.loads(get(url))['chart']['result'][0]
        except Exception as e:  # 다음 호스트로 재시도
            last = e
    raise last


def yahoo(symbol):
    meta = yahoo_chart(symbol, 'range=1d&interval=5m')['meta']
    out = {
        'price': round(float(meta['regularMarketPrice']), 2),
        'prev': round(float(meta['chartPreviousClose']), 2),
        'time': utc_date(meta['regularMarketTime']).strftime('%Y-%m-%dT%H:%M:%SZ'),
    }
    try:
        r = yahoo_chart(symbol, 'range=1mo&interval=1d')
        closes = r['indicators']['quote'][0]['close']
        out['hist'] = [[utc_date(t).strftime('%Y-%m-%d'), round(c, 2)]
                       for t, c in zip(r['timestamp'], closes) if c is not None]
    except Exception as e:
        print('     %s hist skipped: %s' % (symbol, e))
    return out


# ── 미국 ──────────────────────────────────────────

def us_base():
    rs = json.loads(get('https://markets.newyorkfed.org/api/rates/unsecured/effr/last/400.json'))['refRates']
    rs.sort(key=lambda r: r['effectiveDate'])
    cur = rs[-1]
    monthly = {}
    for r in rs:  # 월말 값
        monthly[r['effectiveDate'][:7]] = float(r['targetRateTo'])
    return {
        'lower': float(cur['targetRateFrom']),
        'upper': float(cur['targetRateTo']),
        'date': cur['effectiveDate'],
        'hist': [[m, v] for m, v in sorted(monthly.items())][-13:],
        'change': last_change([[r['effectiveDate'], float(r['targetRateTo'])] for r in rs]),
    }


def us_10y():
    rows = []
    year = dt.datetime.now(dt.timezone.utc).year
    for y in (year, year - 1):  # 연초에는 올해 데이터만으로 한 달이 안 된다
        url = ('https://home.treasury.gov/resource-center/data-chart-center/interest-rates/'
               'daily-treasury-rates.csv/%d/all?type=daily_treasury_yield_curve'
               '&field_tdr_date_value=%d&page&_format=csv') % (y, y)
        for row in csv.DictReader(io.StringIO(get(url))):
            if row.get('10 Yr'):
                rows.append((dt.datetime.strptime(row['Date'], '%m/%d/%Y').date(), float(row['10 Yr'])))
        if len(rows) >= 23:
            break
    rows.sort()
    return {
        'yield': rows[-1][1], 'prev': rows[-2][1], 'date': rows[-1][0].isoformat(),
        'hist': [[d.isoformat(), v] for d, v in rows[-22:]],
    }


# ── 한국 (ECOS) ───────────────────────────────────

def ecos(stat, cycle, start, end, item):
    url = 'https://ecos.bok.or.kr/api/StatisticSearch/%s/json/kr/1/%d/%s/%s/%s/%s/%s' % (
        ECOS_KEY, ECOS_MAX, stat, cycle, start, end, item)
    data = json.loads(get(url))
    rows = data.get('StatisticSearch', {}).get('row') or []
    if not rows:
        raise RuntimeError('ECOS %s %s: no rows (%s)' % (stat, cycle, json.dumps(data, ensure_ascii=False)[:120]))
    return [(r['TIME'], float(r['DATA_VALUE'])) for r in rows]


def ecos_daily(stat, item, days):
    """최근 days일의 일별 값(오래된 순). 비어 있으면 이전 구간으로 넘어간다.
    ECOS는 오래된 순으로 주므로 sample 키(10건)일 때는 구간을 10건 이하로 잡아야 최신 값이 잘리지 않는다."""
    end = dt.datetime.now(KST).date()
    for _ in range(4):
        start = end - dt.timedelta(days=days - 1)
        try:
            return ecos(stat, 'D', start.strftime('%Y%m%d'), end.strftime('%Y%m%d'), item)
        except RuntimeError:
            end = start - dt.timedelta(days=1)
    raise RuntimeError('ECOS %s: no recent rows' % stat)


def iso(t):
    return '%s-%s-%s' % (t[:4], t[4:6], t[6:8]) if len(t) == 8 else '%s-%s' % (t[:4], t[4:6])


def kr_base():
    t, v = ecos_daily('722Y001', '0101000', 9)[-1]
    now = dt.datetime.now(KST).date()
    months = 10 if ECOS_KEY == 'sample' else 24
    first = now.year * 12 + now.month - 1 - (months - 1)
    start = '%04d%02d' % (first // 12, first % 12 + 1)
    hist = [[iso(m), x] for m, x in ecos('722Y001', 'M', start, now.strftime('%Y%m'), '0101000')]
    if hist[-1][0] != now.strftime('%Y-%m'):  # 이번 달 월별 값은 아직 없으므로 현재 값으로 채운다
        hist.append([now.strftime('%Y-%m'), v])
    return {'rate': v, 'date': iso(t), 'hist': hist, 'change': last_change(hist)}


def kr_10y():
    rows = ecos_daily('817Y002', '010210000', 13 if ECOS_KEY == 'sample' else 45)
    if len(rows) < 2:
        rows = ecos_daily('817Y002', '010210000', 20)
    return {
        'yield': rows[-1][1], 'prev': rows[-2][1], 'date': iso(rows[-1][0]),
        'hist': [[iso(t), x] for t, x in rows][-22:],
    }


JOBS = [
    ('brent', lambda: yahoo('BZ=F')),
    ('wti', lambda: yahoo('CL=F')),
    ('usBase', us_base),
    ('us10y', us_10y),
    ('krBase', kr_base),
    ('kr10y', kr_10y),
]


def main(prev_path, out_path):
    try:
        with open(prev_path, encoding='utf-8') as f:
            out = json.load(f)
    except Exception:
        out = {}
    errors = []
    for key, job in JOBS:
        try:
            out[key] = job()
            print('ok   %-6s %s' % (key, json.dumps({k: v for k, v in out[key].items() if k != 'hist'})))
        except Exception as e:
            # URL에 ECOS 키가 들어 있으므로 예외 메시지만 남긴다
            msg = '%s: %s' % (type(e).__name__, str(e).replace(ECOS_KEY, '***'))
            errors.append({'item': key, 'error': msg[:200]})
            print('FAIL %-6s %s (keeping previous)' % (key, msg))
    out['updated'] = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    out['errors'] = errors
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, separators=(',', ':'))
    # 전부 실패했을 때만 실패로 끝내서 Actions에서 바로 보이게 한다
    if len(errors) == len(JOBS):
        sys.exit(1)


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
