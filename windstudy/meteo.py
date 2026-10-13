"""Пробная загрузка POWER: месячный кэш SQLite, почасовая проверка UTC."""
from http.client import IncompleteRead, RemoteDisconnected
from email.utils import parsedate_to_datetime
import calendar
import csv
import hashlib
import json
import math
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from urllib.parse import urlencode
from urllib.request import urlopen
from urllib.error import HTTPError, URLError

PARAMETERS = ('WS10M', 'WS50M')
ENDPOINT = 'https://power.larc.nasa.gov/api/temporal/hourly/point'

class Cancelled(Exception):
    pass


def request_url(cell, year, month):
    return ENDPOINT + '?' + urlencode({
        'parameters': ','.join(PARAMETERS), 'community': 'RE',
        'latitude': cell[3], 'longitude': cell[4],
        'start': f'{year}{month:02d}01',
        'end': f'{year}{month:02d}{calendar.monthrange(year, month)[1]:02d}',
        'format': 'JSON', 'time-standard': 'UTC'})


def fetch(url, cancel, on_retry=None, gate=None):
    """Повторяется весь запрос, включая чтение тела; частичные байты не кэшируются."""
    on_retry = on_retry or (lambda message: None)
    attempts = 6
    for attempt in range(attempts):
        if cancel.is_set():
            raise Cancelled()
        if gate:gate.acquire(cancel,on_retry)
        try:
            try:
                with urlopen(url, timeout=30) as response:
                    return response.read()
            except HTTPError as error:
                if error.code==429 and gate:
                    value=error.headers.get('Retry-After') if error.headers else None
                    seconds=0
                    if value:
                        try:seconds=max(0,float(value))
                        except ValueError:
                            try:seconds=max(0,(parsedate_to_datetime(value)-datetime.now(timezone.utc)).total_seconds())
                            except (ValueError,TypeError,OverflowError):pass
                    delay=gate.penalize(seconds)
                    on_retry(f'Пауза NASA 429: вся очередь остановлена на {delay:.0f} с; параллельность снижена до 1')
                raise
            finally:
                if gate:gate.release()
        except (OSError, URLError, IncompleteRead, RemoteDisconnected) as error:
            if isinstance(error, HTTPError) and error.code not in (408, 429, 500, 502, 503, 504):
                raise
            if attempt == attempts - 1:
                raise ConnectionError(
                    f'NASA POWER: не удалось получить ответ после {attempts} попыток. '
                    f'Кэш сохранён; повторите запуск с той же папкой. Причина: {error}'
                ) from error
            if isinstance(error,HTTPError) and error.code==429 and gate:
                continue  # Следующая попытка проходит через общую паузу.
            delay = min(60, 5 * 2**attempt)
            if isinstance(error, HTTPError):
                retry_after = error.headers.get('Retry-After') if error.headers else None
                if retry_after:
                    try:
                        delay = max(delay, float(retry_after))
                    except ValueError:
                        try:
                            target = parsedate_to_datetime(retry_after)
                            delay = max(delay, (target - datetime.now(timezone.utc)).total_seconds())
                        except (ValueError, TypeError, OverflowError):
                            pass
            on_retry(f'Сетевой сбой: {error}. Повтор {attempt+2}/{attempts} через {delay:g} с')
            if cancel.wait(delay):
                raise Cancelled()


def parse_month(raw, year, month):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'Повторный ключ JSON: {key}')
            result[key] = value
        return result
    data = json.loads(raw, object_pairs_hook=unique_pairs)
    header = data.get('header', {})
    if header.get('time_standard') != 'UTC':
        raise ValueError('Ответ NASA не подтверждает временную шкалу UTC')
    series = data.get('properties', {}).get('parameter', {})
    for parameter in PARAMETERS:
        if not isinstance(series.get(parameter), dict):
            raise ValueError(f'В ответе нет параметра {parameter}')
        units = data.get('parameters', {}).get(parameter, {}).get('units')
        if units != 'm/s':
            raise ValueError(f'Неожиданные единицы {parameter}: {units}')
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    hours = calendar.monthrange(year, month)[1] * 24
    expected = [(start + timedelta(hours=i)).strftime('%Y%m%d%H') for i in range(hours)]
    keys = set(expected)
    if any(set(series[p])-keys for p in PARAMETERS):
        raise ValueError('Ответ содержит время вне запрошенного месяца или неверные метки')
    rows = []
    counts = {p: {'valid': 0, 'missing': 0, 'invalid': 0, 'received': len(series[p])} for p in PARAMETERS}
    fill = header.get('fill_value', -999)
    for key in expected:
        values, flags = [], []
        for p in PARAMETERS:
            value = series[p].get(key)
            if value is None or value == fill:
                value, flag = None, 'missing'
            elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                value, flag = None, 'invalid'
            else:
                value, flag = float(value), 'valid'
            values.append(value); flags.append(flag); counts[p][flag] += 1
        rows.append([datetime.strptime(key, '%Y%m%d%H').replace(tzinfo=timezone.utc).isoformat(), *values, *flags])
    return rows, {'expected': hours, 'parameters': counts,
                  'valid_both': sum(r[3] == r[4] == 'valid' for r in rows)}


def connect(path):
    db = sqlite3.connect(path)
    db.execute('''CREATE TABLE IF NOT EXISTS responses (
        url TEXT PRIMARY KEY, fetched_utc TEXT NOT NULL, raw BLOB NOT NULL, sha256 TEXT NOT NULL)''')
    return db


def run_pilot(sample, count, year, folder, cancel=None, progress=None, downloader=fetch):
    if type(count) is not int or not 1 <= count <= 5 or count > len(sample['cells']):
        raise ValueError('Пробная загрузка: от 1 до 5 ячеек из выборки')
    if type(year) is not int or not 2001 <= year < datetime.now(timezone.utc).year:
        raise ValueError('Выберите завершённый год, начиная с 2001')
    cancel = cancel or Event()
    progress = progress or (lambda done, total, message: None)
    folder = Path(folder); folder.mkdir(parents=True, exist_ok=True)
    report = {'schema_version': 1, 'year': year, 'time_standard': 'UTC',
              'pool_checksum': sample['pool_checksum'], 'selection_sha256': hashlib.sha256(
                  json.dumps(sample, sort_keys=True).encode()).hexdigest(),
              'requested_cells': sample['cells'][:count], 'cells': [], 'status': 'running',
              'started_utc': datetime.now(timezone.utc).isoformat()}
    started = time.monotonic(); done = 0; total = count*12
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
    report_path = folder / f'report_{year}_{stamp}.json'
    def persist():
        report['elapsed_seconds'] = round(time.monotonic()-started, 2)
        temporary = report_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(report_path)
    db = connect(folder/'meteo_cache.sqlite')
    try:
        for cell in sample['cells'][:count]:
            item = {'cell_id': cell[0], 'latitude': cell[3], 'longitude': cell[4], 'months': [],
                    'expected_hours': (366 if calendar.isleap(year) else 365)*24}
            report['cells'].append(item); year_rows = []
            for month in range(1,13):
                if cancel.is_set():
                    raise Cancelled()
                url = request_url(cell,year,month)
                progress(done,total,f"{cell[0]} · {year}-{month:02d}")
                entry = {'month':month, 'url':url}
                try:
                    cached = db.execute('SELECT raw,sha256,fetched_utc FROM responses WHERE url=?',(url,)).fetchone()
                    if cached:
                        raw, sha, fetched = cached
                        if hashlib.sha256(raw).hexdigest() != sha:
                            raise ValueError('Повреждён кэш: удалите соответствующий ответ или выберите новую папку')
                        rows, quality = parse_month(raw,year,month)
                        source = 'cache'
                    else:
                        raw = downloader(url,cancel)
                        rows, quality = parse_month(raw,year,month)
                        sha = hashlib.sha256(raw).hexdigest()
                        fetched = datetime.now(timezone.utc).isoformat()
                        with db:
                            db.execute('INSERT OR REPLACE INTO responses VALUES (?,?,?,?)',(url,fetched,raw,sha))
                        source = 'network'
                    year_rows.extend(rows)
                    entry.update(status='checked',source=source,bytes=len(raw),sha256=sha,fetched_utc=fetched,quality=quality)
                except Cancelled:
                    raise
                except Exception as error:
                    entry.update(status='error', error=str(error))
                    start = datetime(year, month, 1, tzinfo=timezone.utc)
                    year_rows.extend([[(start+timedelta(hours=h)).isoformat(),None,None,'unavailable','unavailable']
                                      for h in range(calendar.monthrange(year,month)[1]*24)])
                item['months'].append(entry); done += 1
                item['valid_both'] = sum(m.get('quality',{}).get('valid_both',0) for m in item['months'])
                item['checked_hours'] = sum(m.get('quality',{}).get('expected',0) for m in item['months'])
                persist(); progress(done,total, f"{cell[0]} · месяц {month}: {entry['status']}")
                if entry.get('source') == 'network' and cancel.wait(.25):
                    raise Cancelled()
            item['complete'] = item['valid_both'] == item['expected_hours']
            # Уникальный файл запуска: неполный результат не перезаписывает предыдущий.
            csv_path = folder/f"{cell[0].replace(':','_')}_{year}_{stamp}.csv"
            with csv_path.open('w',newline='',encoding='utf-8-sig') as stream:
                writer = csv.writer(stream)
                writer.writerow(['time_utc','WS10M_m_s','WS50M_m_s','WS10M_status','WS50M_status'])
                writer.writerows(year_rows)
            item['csv'] = csv_path.name
        report['status'] = 'complete' if all(c['complete'] for c in report['cells']) else 'incomplete'
    except Cancelled:
        report['status'] = 'cancelled'
    finally:
        db.close(); persist()
    report['report_path'] = str(report_path)
    return report
