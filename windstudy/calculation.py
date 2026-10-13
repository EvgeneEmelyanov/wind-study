"""Полностью автономный расчёт выбранного диапазона: сетевых вызовов нет."""
from pathlib import Path
from dataclasses import asdict
from datetime import datetime,timezone
import numpy as np
from .weather_store import dates,db_open,read_window
from .experiments import atomic_json,wind_at_height,statistics
from .meteo import Cancelled
from .approximation import normalized_power,CurveSettings,FORMULA_VERSION


def calculate(plan,start,end,height,models,folder,output,cancel,progress,check_only=False):
    a,b=dates(start,end)
    if not models:raise ValueError('Нет включённых моделей')
    if not np.isfinite(height) or height<=0:raise ValueError('Неверная высота')
    for model in models:model.validate()
    output=Path(output);output.mkdir(parents=True,exist_ok=True);atomic_json(output/'plan.json',plan)
    report={'status':'checking','year':f'{a} — {b}','period_start':str(a),'period_end':str(b),
            'expected_hours':((b-a).days+1)*24,'height_m':height,'models':[asdict(m) for m in models],
            'formula':FORMULA_VERSION,'cells':[],'errors':[],'source':'NASA POWER, UTC',
            'started_utc':datetime.now(timezone.utc).isoformat(),'density_correction':'none'}
    db=db_open(folder);n=len(plan['cells'])
    try:
        # Сначала проверяются все ячейки. При пробелах не начинаем вычисление моделей.
        for i,c in enumerate(plan['cells']):
            if cancel.is_set():raise Cancelled()
            progress(i,2*n,'Проверка данных: '+c[0])
            try:
                wind,_=read_window(db,folder,c[0],a,b);_,valid=wind_at_height(wind,height)
                if not valid.all():raise ValueError(f'{np.sum(~valid)} непригодных часов на выбранной высоте')
            except Exception as error:report['errors'].append(f'{c[0]}: {error}')
        if report['errors']:
            report['status']='incomplete';return report
        if check_only:
            report['status']='ready';return report
        report['status']='calculating';lookup={}
        for i,c in enumerate(plan['cells']):
            if cancel.is_set():raise Cancelled()
            wind,sources=read_window(db,folder,c[0],a,b);v,valid=wind_at_height(wind,height)
            if not valid.all():raise ValueError('Кэш изменился после проверки: '+c[0])
            results=[]
            for model in models:
                p=model.power(v) if hasattr(model,'power') else normalized_power(v,model.exponent,CurveSettings(model.v_min,model.v_nom,model.v_max))
                results.append({'cf':float(p.mean()),'hours_equivalent':float(p.sum()),'energy_kwh':float(p.sum()*model.nominal_kw)})
            lookup[c[0]]=results
            report['cells'].append({'id':c[0],'lat':c[3],'lon':c[4],'hours':len(v),'sources':sources,'results':results})
            progress(n+i+1,2*n,'Расчёт: '+c[0])
        means=[];medians=[]
        for experiment in plan['plans']:
            if cancel.is_set():raise Cancelled()
            matrix=np.array([[r['cf'] for r in lookup[cid]] for cid in experiment['ids']])
            means.append(matrix.mean(axis=0).tolist());medians.append(np.median(matrix,axis=0).tolist())
        report.update(status='complete',means=means,medians=medians,
                      statistics={key:[statistics(np.array(values)[:,j]) for j in range(len(models))]
                                  for key,values in [('means',means),('medians',medians)]})
    except Cancelled:report['status']='cancelled'
    except Exception as error:report['status']='incomplete';report['errors'].append(str(error))
    finally:db.close();atomic_json(output/'result.json',report)
    return report
