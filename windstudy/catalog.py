"""Версионируемый каталог, линейная интерполяция и безопасные выражения."""
import ast
from dataclasses import dataclass
import json
from pathlib import Path
import numpy as np
from .approximation import CurveSettings


def expression_value(expression,parameters,v):
    tree=ast.parse(expression,mode='eval')
    if len(list(ast.walk(tree)))>100:raise ValueError('Слишком длинная формула')
    def evaluate(node):
        if isinstance(node,ast.Expression):return evaluate(node.body)
        if isinstance(node,ast.Constant) and type(node.value) in (int,float):return node.value
        if isinstance(node,ast.Name):
            if node.id=='v':return v
            if node.id in parameters:return float(parameters[node.id])
            raise ValueError('Неизвестный параметр: '+node.id)
        if isinstance(node,ast.UnaryOp) and isinstance(node.op,(ast.UAdd,ast.USub)):
            return evaluate(node.operand)*(1 if isinstance(node.op,ast.UAdd) else -1)
        if isinstance(node,ast.BinOp):
            a,b=evaluate(node.left),evaluate(node.right)
            if isinstance(node.op,ast.Add):return a+b
            if isinstance(node.op,ast.Sub):return a-b
            if isinstance(node.op,ast.Mult):return a*b
            if isinstance(node.op,ast.Div):return a/b
            if isinstance(node.op,ast.Pow):
                if np.ndim(b)!=0 or abs(b)>20:raise ValueError('Степень должна быть константой/параметром от -20 до 20')
                return np.power(a,b)
        raise ValueError('Разрешены числа, параметры, v, скобки и операции + - * / **')
    with np.errstate(all='ignore'):return evaluate(tree)

@dataclass
class CatalogModel:
    exponent: str
    name: str
    nominal_kw: float
    specification: dict

    def validate(self):
        if self.nominal_kw is None or not np.isfinite(self.nominal_kw) or self.nominal_kw<=0:
            raise ValueError(f'{self.name}: задайте номинальную мощность')
        s=self.specification
        if 'expression' in s:
            p=s['parameters'];CurveSettings(p['v_min'],p['v_nom'],p['v_max']).validate()
            if s.get('boundary_rule')!='zero_ramp_one_zero_inclusive_cutout':raise ValueError('Неизвестное правило участков')
            self.power(np.linspace(p['v_min'],p['v_nom'],1001,endpoint=False))
        else:
            points=self.valid_points()
            if s.get('below_table') not in ('zero','hold') or s.get('above_table') not in ('zero','hold'):
                raise ValueError(f'{self.name}: задайте below_table и above_table (zero/hold); не угадываем пустые клетки')

    def valid_points(self):
        raw=self.specification['points']
        speeds=[p[0] for p in raw]
        if len(set(speeds))!=len(speeds):raise ValueError(f'{self.name}: повторные скорости')
        if any(not np.isfinite(v) or v<0 for v in speeds):raise ValueError('Некорректная скорость')
        points=sorted((v,p) for v,p in raw if p is not None)
        if len(points)<2 or any(not np.isfinite(p) or p<0 for _,p in points):raise ValueError('Нужны минимум две корректные точки мощности')
        lo,hi=points[0][0],points[-1][0]
        if any(p is None and lo<v<hi for v,p in raw):raise ValueError('Внутри кривой есть пустая точка; заполните или явно удалите строку для интерполяции')
        return np.array(points,dtype=float)

    def power(self,speed):
        v=np.asarray(speed,dtype=float);s=self.specification
        if 'expression' in s:
            p=s['parameters'];out=np.zeros_like(v);mask=(v>=p['v_min'])&(v<p['v_nom'])
            if mask.any():
                values=expression_value(s['expression'],p,v[mask])
                if np.any(~np.isfinite(values)) or np.any(np.asarray(values)<0) or np.any(np.asarray(values)>1):
                    raise ValueError(f'{self.name}: формула даёт p вне [0,1] или нечисловое значение')
                out[mask]=values
            out[(v>=p['v_nom'])&(v<=p['v_max'])]=1
            return out
        points=self.valid_points();left=0 if s['below_table']=='zero' else points[0,1]
        right=0 if s['above_table']=='zero' else points[-1,1]
        # Значения выше номинала сохраняются; КИУМ не обрезается искусственно.
        return np.interp(v,points[:,0],points[:,1],left=left,right=right)/self.nominal_kw


def load_catalog(path):
    data=json.loads(Path(path).read_text(encoding='utf-8'))
    if data.get('schema_version')!=1:raise ValueError('Неподдерживаемая версия каталога')
    ids=[]
    for key in ('formulas','curves'):
        for entry in data[key]:
            cid=entry['id']
            if not isinstance(cid,str) or not cid or len(cid)>20 or any(c in cid for c in '[]:*?/\\'):
                raise ValueError('ID: 1–20 символов без []:*?/\\')
            ids.append(cid)
            if entry.get('enabled'):
                CatalogModel(cid,entry['name'],entry['nominal_kw'],entry).validate()
    if len(ids)!=len(set(ids)):raise ValueError('ID в каталоге должны быть уникальными')
    return data


def enabled_models(data):
    return [CatalogModel(e['id'],e['name'],e['nominal_kw'],e) for key in ('formulas','curves') for e in data[key] if e.get('enabled')]
