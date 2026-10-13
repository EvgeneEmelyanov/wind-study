"""Редактор каталога: метаданные JSON и таблица точек с вставкой из Excel."""
import copy
import json
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QComboBox,QPushButton,
    QPlainTextEdit,QTableWidget,QTableWidgetItem,QLabel,QMessageBox,QApplication)
from .catalog import load_catalog

class CatalogDialog(QDialog):
    def __init__(self,path,parent=None):
        super().__init__(parent);self.path=path;self.data=load_catalog(path);self.entries=[]
        self.setWindowTitle('Каталог характеристик и формул');self.resize(950,760)
        layout=QVBoxLayout(self);self.choice=QComboBox();layout.addWidget(self.choice)
        layout.addWidget(QLabel('Редактируйте метаданные JSON. enabled=true включает модель в расчёт.\n'
                                'Кривые: nominal_kw, below_table и above_table обязательны для включения.\n'
                                'Сохраните запись перед переключением. Формулы: expression и parameters.'))
        self.editor=QPlainTextEdit();layout.addWidget(self.editor)
        self.table=QTableWidget(0,2);self.table.setHorizontalHeaderLabels(['Скорость, м/с','Мощность, кВт (пусто = неизвестно)'])
        self.table.setColumnWidth(0,180);self.table.setColumnWidth(1,500);layout.addWidget(self.table)
        buttons=QHBoxLayout()
        for text,slot in [('Добавить ВЭУ',lambda:self.add(False)),('Добавить формулу',lambda:self.add(True)),
                          ('Вставить 2 столбца',self.paste),('Добавить строку',lambda:self.table.insertRow(self.table.rowCount())),
                          ('Сохранить запись',self.save)]:
            b=QPushButton(text);b.clicked.connect(slot);buttons.addWidget(b)
        layout.addLayout(buttons);self.choice.currentIndexChanged.connect(self.display);self.refresh()
    def refresh(self,index=0):
        self.choice.blockSignals(True);self.choice.clear();self.entries=[]
        for kind in ('formulas','curves'):
            for entry in self.data[kind]:
                self.entries.append((kind,entry));self.choice.addItem(entry['name']+' ['+entry['id']+']')
        self.choice.setCurrentIndex(index);self.choice.blockSignals(False);self.display(index)
    def display(self,index):
        if index<0:return
        kind,entry=self.entries[index]
        self.editor.setPlainText(json.dumps({k:v for k,v in entry.items() if k!='points'},ensure_ascii=False,indent=2))
        points=entry.get('points',[]);self.table.setEnabled(kind=='curves');self.table.setRowCount(len(points))
        for row,(v,p) in enumerate(points):
            self.table.setItem(row,0,QTableWidgetItem(str(v)));self.table.setItem(row,1,QTableWidgetItem('' if p is None else str(p)))
    def add(self,formula):
        kind='formulas' if formula else 'curves';number=len(self.entries)+1
        if formula:
            e={'id':f'formula_{number}','name':'Новая формула','enabled':False,'nominal_kw':1000,
               'expression':'(v-v_min)/(v_nom-v_min)','parameters':{'v_min':3,'v_nom':12.5,'v_max':25},
               'boundary_rule':'zero_ramp_one_zero_inclusive_cutout'}
        else:e={'id':f'wtg_{number}','name':'Новая ВЭУ','enabled':False,'nominal_kw':None,'hub_height_m':None,
                'below_table':'unspecified','above_table':'unspecified','source':'','points':[]}
        self.data[kind].append(e);self.refresh(next(i for i,x in enumerate([*self.data['formulas'],*self.data['curves']]) if x is e))
    def paste(self):
        if not self.table.isEnabled():return
        rows=[line.split('\t') for line in QApplication.clipboard().text().strip().splitlines()]
        try:
            points=[]
            for row in rows:
                if len(row)!=2:raise ValueError('Нужны два столбца без заголовков: скорость и мощность')
                points.append([float(row[0].replace(',','.')),None if not row[1].strip() else float(row[1].replace(',','.'))])
            self.table.setRowCount(len(points))
            for i,(v,p) in enumerate(points):
                self.table.setItem(i,0,QTableWidgetItem(str(v)));self.table.setItem(i,1,QTableWidgetItem('' if p is None else str(p)))
        except Exception as error:QMessageBox.warning(self,'Вставка',str(error))
    def save(self):
        index=self.choice.currentIndex();kind,old=self.entries[index]
        try:
            new=json.loads(self.editor.toPlainText())
            if kind=='curves':
                points=[]
                for row in range(self.table.rowCount()):
                    v=self.table.item(row,0);p=self.table.item(row,1)
                    if v is None or not v.text().strip():continue
                    points.append([float(v.text().replace(',','.')),None if p is None or not p.text().strip() else float(p.text().replace(',','.'))])
                new['points']=points
            data=copy.deepcopy(self.data);pos=self.data[kind].index(old);data[kind][pos]=new
            tmp=self.path.with_suffix('.tmp.json');tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
            load_catalog(tmp);tmp.replace(self.path);self.data=data;self.refresh(index)
            QMessageBox.information(self,'Сохранено','Каталог сохранён. Для расчёта заново подготовьте эксперименты.')
        except Exception as error:QMessageBox.warning(self,'Не сохранено',str(error))
