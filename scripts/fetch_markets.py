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


def get(url, timeout=30):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': '*/*'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8-sig')


def iso_utc(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def yahoo(symbol):
    last = None
    for host in ('query1', 'query2'):
        try:
            url = 'https://%s.finance.yahoo.com/v8/finance/chart/%s?range=1d&interval=5m' % (
                host, urllib.request.quote(symbol))
            meta = json.loads(get(url))['chart']['result'][0]['meta']
            return {
                'price': round(float(meta['regularMarketPrice']), 2),
                'prev': round(float(meta['chartPreviousClose']), 2),
                'time': iso_utc(meta['regularMarketTime']),
            }
        except Exception as e:  # 다음 호스트로 재시도
            last = e
    raise last


def us_base():
    r = json.loads(get('https://markets.newyorkfed.org/api/rates/unsecured/effr/last/1.json'))['refRates'][0]
    return {'lower': float(r['targetRateFrom']), 'upper': float(r['targetRateTo']), 'date': r['effectiveDate']}


def us_10y():
    rows = []
    year = dt.datetime.now(dt.timezone.utc).year
    for y in (year, year - 1):  # 연초에는 올해 데이터가 1건 이하일 수 있다
        url = ('https://home.treasury.gov/resource-center/data-chart-center/interest-rates/'
               'daily-treasury-rates.csv/%d/all?type=daily_treasury_yield_curve'
               '&field_tdr_date_value=%d&page&_format=csv') % (y, y)
        for row in csv.DictReader(io.StringIO(get(url))):
            if row.get('10 Yr'):
                rows.append((dt.datetime.strptime(row['Date'], '%m/%d/%Y').date(), float(row['10 Yr'])))
        if len(rows) >= 2:
            break
    rows.sort(reverse=True)
    return {'yield': rows[0][1], 'prev': rows[1][1], 'date': rows[0][0].isoformat()}


def ecos_rows(stat, item, days=9):
    """최근 기간의 일별 값을 오래된 순으로. sample 키는 10건까지만 주므로 기간을 짧게 나눠 조회한다."""
    end = dt.datetime.now(KST).date()
    for _ in range(4):
        start = end - dt.timedelta(days=days - 1)
        url = 'https://ecos.bok.or.kr/api/StatisticSearch/%s/json/kr/1/10/%s/D/%s/%s/%s' % (
            ECOS_KEY, stat, start.strftime('%Y%m%d'), end.strftime('%Y%m%d'), item)
        data = json.loads(get(url))
        rows = data.get('StatisticSearch', {}).get('row') or []
        if rows:
            return [(r['TIME'], float(r['DATA_VALUE'])) for r in rows]
        end = start - dt.timedelta(days=1)
    raise RuntimeError('ECOS %s: no rows (%s)' % (stat, json.dumps(data, ensure_ascii=False)[:120]))


def ecos_date(t):
    return '%s-%s-%s' % (t[:4], t[4:6], t[6:8])


def kr_base():
    t, v = ecos_rows('722Y001', '0101000')[-1]
    return {'rate': v, 'date': ecos_date(t)}


def kr_10y():
    rows = ecos_rows('817Y002', '010210000')
    if len(rows) < 2:
        rows = ecos_rows('817Y002', '010210000', days=20)
    return {'yield': rows[-1][1], 'prev': rows[-2][1], 'date': ecos_date(rows[-1][0])}


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
            print('ok   %-6s %s' % (key, json.dumps(out[key])))
        except Exception as e:
            # URL에 ECOS 키가 들어 있으므로 예외 메시지만 남긴다
            msg = '%s: %s' % (type(e).__name__, str(e).replace(ECOS_KEY, '***'))
            errors.append({'item': key, 'error': msg[:200]})
            print('FAIL %-6s %s (keeping previous)' % (key, msg))
    out['updated'] = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    out['errors'] = errors
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    # 전부 실패했을 때만 실패로 끝내서 Actions에서 바로 보이게 한다
    if len(errors) == len(JOBS):
        sys.exit(1)


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
