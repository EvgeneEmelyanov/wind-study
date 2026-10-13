"""Переносимый экспорт отчёта в OOXML без Excel, Node.js и внешних библиотек.

Книга — снимок расчёта. Все численные значения рассчитаны ядром и записаны
числами; изменение ячеек книги не запускает повторное моделирование.
"""
from pathlib import Path
from zipfile import ZipFile,ZIP_DEFLATED
from xml.sax.saxutils import escape
import math
import os
import tempfile


def column(index):
    result=''
    while index:
        index,rem=divmod(index-1,26);result=chr(65+rem)+result
    return result


def write_xlsx(path,sheets):
    """Потоковая запись; длинные таблицы делятся на листы вызывающим кодом."""
    ns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    path=Path(path);fd,tmp=tempfile.mkstemp(suffix='.xlsx',dir=path.parent);os.close(fd)
    try:
        with ZipFile(tmp,'w',ZIP_DEFLATED) as z:
            overrides=[];names=[];rels=[]
            for i,(name,headers,rows) in enumerate(sheets,1):
                names.append(f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>')
                rels.append(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>')
                overrides.append(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
                with z.open(f'xl/worksheets/sheet{i}.xml','w') as stream:
                    def emit(text):stream.write(text.encode('utf-8'))
                    emit(f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="{ns}"><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><cols>')
                    for c in range(1,len(headers)+1):
                        width=110 if name=='Сводка' and c==2 else (42 if c==1 else max(18,min(32,len(headers[c-1])+3)))
                        emit(f'<col min="{c}" max="{c}" width="{width}" customWidth="1"/>')
                    emit('</cols><sheetData>')
                    def row_xml(values,r,header=False):
                        height=32 if header else (60 if name=='Сводка' and any(isinstance(v,str) and len(v)>80 for v in values) else 20)
                        emit(f'<row r="{r}" ht="{height}" customHeight="1">')
                        for c,value in enumerate(values,1):
                            if value is None:continue
                            ref=f'{column(c)}{r}'
                            if isinstance(value,(int,float)) and not isinstance(value,bool):
                                if not math.isfinite(value):raise ValueError('Нечисловое значение в отчёте')
                                emit(f'<c r="{ref}" s="{2 if isinstance(value,float) else 0}"><v>{value}</v></c>')
                            else:emit(f'<c r="{ref}" s="{1 if header else 0}" t="inlineStr"><is><t xml:space="preserve">{escape(str(value))}</t></is></c>')
                        emit('</row>')
                    row_xml(headers,1,True);last=1
                    for last,values in enumerate(rows,2):
                        if last>1048576:raise ValueError('Превышен предел строк Excel')
                        row_xml(values,last)
                    emit(f'</sheetData><autoFilter ref="A1:{column(len(headers))}{last}"/></worksheet>')
            z.writestr('xl/workbook.xml',f'<workbook xmlns="{ns}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'+''.join(names)+'</sheets></workbook>')
            z.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'+''.join(rels)+f'<Relationship Id="rId{len(names)+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
            z.writestr('_rels/.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
            z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'+''.join(overrides)+'</Types>')
            z.writestr('xl/styles.xml',f'''<styleSheet xmlns="{ns}"><numFmts count="1"><numFmt numFmtId="164" formatCode="0.00000000"/></numFmts><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF203B51"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs><cellXfs count="3"><xf fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment wrapText="1" vertical="top"/></xf><xf fontId="1" fillId="2" borderId="0" xfId="0" applyAlignment="1"><alignment wrapText="1" vertical="center"/></xf><xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>''')
        os.replace(tmp,path)
    finally:Path(tmp).unlink(missing_ok=True)


def export_report(plan,report,path):
    complete=report['status']=='complete';unique=len(plan['cells'])
    summary=[['Статус',report['status']],['Период UTC',report['year']],['Высота, м',report['height_m']],
             ['Точек в эксперименте',plan['n']],['Экспериментов',plan['repetitions']],
             ['Всего включений',plan['n']*plan['repetitions']],['Уникальных ячеек',unique],
             ['Пул',plan['pool_size']],['Покрытие пула, %',100*unique/plan['pool_size']],
             ['Не использовано',plan['pool_size']-unique],['Пул SHA256',plan['pool_checksum']],
             ['Формула',report['formula']],['Источник','NASA POWER, UTC; локальный архив AWS и API-кэш; источники и хеши в result.json'],
             ['Метод','Рандомизированный Соболь с фильтрацией по пулу; seed = base_seed + номер - 1'],
             ['Высота','50 м: WS50M; другая: почасовой alpha по WS10M/WS50M, без ограничения alpha'],
             ['Статистика','Дисперсия и СКО выборочные (R-1); при R=1 не определены. Квантили: линейная интерполяция.'],
             ['Единицы','КИУМ в таблицах: доля 0..1. Выработка: кВт·ч.'],
             ['Примечание','Снимок результата, не интерактивная расчётная книга. Это разброс по географии, не ошибка относительно реальных ВЭУ.'],
             ['Почасовые данные','В папке кэша (архив NPZ / SQLite); здесь агрегаты по ячейкам и экспериментам'],
             ['Ошибки','; '.join(report['errors'])]]
    sheets=[('Сводка',['Показатель','Значение'],summary)]
    if complete:
        for key,title in [('means','Средние'),('medians','Медианы')]:
            sheets.append((title,['Эксперимент','Seed']+[f'Модель {m["exponent"]}, КИУМ' for m in report['models']],
                           [[i+1,exp['seed'],*report[key][i]] for i,exp in enumerate(plan['plans'])]))
        labels={'count':'Количество','mean':'Среднее','median':'Медиана','min':'Минимум','max':'Максимум',
                'range':'Размах','variance_sample':'Выборочная дисперсия','std_sample':'Выборочное СКО',
                'q05':'P05','q25':'Q1','q75':'Q3','q95':'P95','iqr':'Межквартильный размах'}
        rows=[]
        for key,title in [('means','Средние'),('medians','Медианы')]:
            for j,m in enumerate(report['models']):
                for metric,value in report['statistics'][key][j].items():rows.append([title,m['exponent'],labels[metric],value])
        sheets.append(('Статистика',['Выборка','Модель','Показатель','Значение'],rows))
    sheets.append(('Модели',['Степень','Vmin, м/с','Vном, м/с','Vmax, м/с','Pном, кВт'],
                   [[m.get(k,m.get('specification',{}).get('parameters',{}).get(k)) for k in ['exponent','v_min','v_nom','v_max','nominal_kw']] for m in report['models']]))
    import json
    sheets.append(('Каталог снимок',['ID','Имя','Спецификация JSON'],[[m['exponent'],m.get('name',''),json.dumps(m,ensure_ascii=False)] for m in report['models']]))
    for j,m in enumerate(report['models']):
        rows=[[c['id'],c['lat'],c['lon'],c['hours'],c['results'][j]['cf'],c['results'][j]['hours_equivalent'],c['results'][j]['energy_kwh']] for c in report['cells']]
        sheets.append((f'Модель {m["exponent"]}',['ID','Широта','Долгота','Часов','КИУМ, доля','Эквивалентные часы','Выработка, кВт·ч'],rows))
    sheets.append(('Частота',['ID','Широта','Долгота','Экспериментов'],[[c[0],c[3],c[4],plan['frequency'][c[0]]] for c in plan['cells']]))
    sheets.append(('Покрытие',['После эксперимента','Уникальных ячеек','Покрытие, %'],[[i+1,n,100*n/plan['pool_size']] for i,n in enumerate(plan['coverage'])]))
    # Части по 500 тыс. строк обходят предел Excel без потери состава экспериментов.
    batch=[];part=1
    for exp in plan['plans']:
        for order,cid in enumerate(exp['ids'],1):
            batch.append([exp['experiment'],exp['seed'],order,cid])
            if len(batch)==500000:
                sheets.append((f'Состав {part}',['Эксперимент','Seed','Порядок','ID'],batch));batch=[];part+=1
    if batch:sheets.append((f'Состав {part}',['Эксперимент','Seed','Порядок','ID'],batch))
    write_xlsx(path,sheets)
