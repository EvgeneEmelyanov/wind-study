"""Проверить нужный период, дозагрузить недостающее, рассчитать всю серию."""
from pathlib import Path
from .calculation import calculate
from .downloading import download
from .experiments import atomic_json

def run(plan,start,end,height,models,folder,output,cancel,progress,workers=1,downloader=None):
    checked=calculate(plan,start,end,height,models,folder,output,cancel,progress,check_only=True)
    if checked['status']=='cancelled':return checked
    loading=None
    if checked['status']!='ready':
        progress(0,1,'Дозагрузка данных выбранных экспериментов за расчётный период…')
        missing_ids={e.split(': ',1)[0] for e in checked['errors']}
        needed=[c for c in plan['cells'] if c[0] in missing_ids]
        loading=download(needed,start,end,folder,cancel,progress,workers,downloader)
    result=calculate(plan,start,end,height,models,folder,output,cancel,progress)
    if loading:
        result['download']=loading
        if result['status']!='complete':
            result['errors'].extend('Загрузка: '+e['error'] for e in loading['errors'])
        atomic_json(Path(output)/'result.json',result)
    return result
