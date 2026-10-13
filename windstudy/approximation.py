"""Три степенные характеристики по формуле, предоставленной пользователем."""
import calendar
import csv
import hashlib
import json
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import numpy as np

FORMULA_VERSION = 'power-difference-1-2-3-v1'

@dataclass(frozen=True)
class CurveSettings:
    v_min: float = 3.0
    v_nom: float = 12.5
    v_max: float = 25.0

    def validate(self):
        if not all(np.isfinite(v) for v in (self.v_min,self.v_nom,self.v_max)):
            raise ValueError('Скорости должны быть конечными числами')
        if not 0 <= self.v_min < self.v_nom < self.v_max:
            raise ValueError('Требуется 0 ≤ Vmin < Vном < Vmax')


def normalized_power(speed, exponent, settings=CurveSettings()):
    settings.validate()
    if exponent not in (1,2,3):
        raise ValueError('Поддерживаются степени 1, 2 и 3')
    v = np.asarray(speed,dtype=float)
    if not np.all(np.isfinite(v)) or np.any(v<0):
        raise ValueError('Скорость должна быть конечной и неотрицательной')
    power = np.zeros_like(v)
    ramp = (v >= settings.v_min) & (v < settings.v_nom)
    power[ramp] = ((v[ramp]**exponent-settings.v_min**exponent) /
                   (settings.v_nom**exponent-settings.v_min**exponent))
    power[(v>=settings.v_nom)&(v<=settings.v_max)] = 1.0
    return power


def calculate_csv(path, settings=CurveSettings()):
    """Полный календарный год UTC на высоте 50 м. Неполные ряды отклоняются."""
    settings.validate(); path=Path(path); raw=path.read_bytes()
    with path.open(encoding='utf-8-sig',newline='') as stream:
        rows=list(csv.DictReader(stream))
    if not rows:
        raise ValueError('Пустой метеофайл')
    timestamps=[]; speeds=[]
    for i,row in enumerate(rows,2):
        try:
            stamp=datetime.fromisoformat(row['time_utc'])
            if stamp.utcoffset()!=timedelta(0):
                raise ValueError('Ожидается UTC')
            speed=float(row['WS50M_m_s'])
            if row['WS50M_status']!='valid' or not np.isfinite(speed) or speed<0:
                raise ValueError('Непригодная скорость на 50 м')
            timestamps.append(stamp); speeds.append(speed)
        except (KeyError,TypeError,ValueError) as error:
            raise ValueError(f'Строка {i}: неполные или неверные данные WS50M/UTC ({error})') from error
    year=timestamps[0].year
    expected=(366 if calendar.isleap(year) else 365)*24
    start=datetime(year,1,1,tzinfo=timezone.utc)
    if len(rows)!=expected or any(t!=start+timedelta(hours=i) for i,t in enumerate(timestamps)):
        raise ValueError('Нужен полный год UTC: последовательные часы с 1 января, без повторов и пропусков')
    results=[]
    for n in (1,2,3):
        p=normalized_power(speeds,n,settings)
        results.append({'exponent':n,'capacity_factor':float(p.mean()),
                        'equivalent_full_load_hours':float(p.sum())})
    return {'file':path.name,'source_sha256':hashlib.sha256(raw).hexdigest(),
            'year':year,'hours':expected,'height_m':50,'time_standard':'UTC',
            'settings':asdict(settings),'formula_version':FORMULA_VERSION,
            'density_correction':'none; density is not an input to the supplied formula',
            'results':results}


def save_results(results,path):
    Path(path).write_text(json.dumps({'schema_version':1,'calculations':results},
                                   ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
