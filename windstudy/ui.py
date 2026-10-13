"""Подготовка, загрузка и автономный расчёт — три независимых этапа."""
from pathlib import Path
from datetime import datetime, date, timedelta, timezone
from threading import Event
from PySide6.QtCore import QThread, Signal, QSettings, QDate
from PySide6.QtWidgets import (QApplication,QMainWindow,QWidget,QVBoxLayout,QHBoxLayout,QFormLayout,
    QLabel,QSpinBox,QDoubleSpinBox,QLineEdit,QPushButton,QFileDialog,QPlainTextEdit,QProgressBar,
    QMessageBox,QDateEdit,QCheckBox,QComboBox)
from .pool import load
from .experiments import make_plan
from .excel import export_report
from .meteo import Cancelled
from .weather_store import inventory, dates
from .downloading import download
from .workflow import run as calculate

ROOT=Path(__file__).resolve().parents[1]

class Task(QThread):
    progress=Signal(int,int,str)
    result=Signal(object)
    failed=Signal(str)
    def __init__(self,operation):
        super().__init__();self.operation=operation;self.cancel=Event()
    def run(self):
        try:self.result.emit(self.operation(self.cancel,self.progress.emit))
        except Cancelled:self.failed.emit('Операция отменена')
        except Exception as error:self.failed.emit(str(error))

class Window(QMainWindow):
    def __init__(self):
        super().__init__();self.plan=None;self.task=None;self.settings=QSettings('WindStudy','Experiments')
        self.setWindowTitle('ВЭУ — серия экспериментов · 1.3.2');self.resize(1100,850)
        root=QWidget();layout=QVBoxLayout(root);form=QFormLayout();self.fields=[]
        self.n=self.spin(1,9172,512);self.r=self.spin(1,10000,100);self.seed=self.spin(0,2147480000,42)
        self.height=QDoubleSpinBox();self.height.setRange(1,300);self.height.setValue(50);self.fields.append(self.height)
        for label,widget in [('Точек в эксперименте',self.n),('Экспериментов',self.r),('Начальный seed',self.seed),('Общая высота, м',self.height)]:form.addRow(label,widget)
        self.download_from,self.download_to=self.date_row(form,'Скачать: от / до (UTC)','download','2001-01-01','2025-12-31')
        self.calc_from,self.calc_to=self.date_row(form,'Рассчитать: от / до (UTC)','calculate','2020-01-01','2024-12-31')
        self.all_pool=QCheckBox('Скачать для всего пула (без подготовки экспериментов)');self.all_pool.setChecked(True)
        self.all_pool.toggled.connect(lambda *_:self.update_buttons());self.fields.append(self.all_pool);form.addRow(self.all_pool)
        self.download_mode=QComboBox();self.download_mode.addItems(['Архив AWS → на диск (для больших объёмов)','Точечный API (для небольших интервалов)']);self.fields.append(self.download_mode)
        form.addRow('Способ скачивания',self.download_mode)
        self.workers=QSpinBox();self.workers.setRange(1,5);self.workers.setValue(1);self.fields.append(self.workers)
        form.addRow('Параллельных запросов',self.workers);layout.addLayout(form)
        self.pool_path=self.path_row(layout,'Пул (один раз)',str(ROOT/'data/pool.sqlite'),False)
        self.cache=self.path_row(layout,'Папка кэша',str(ROOT/'cache'),True)
        self.output=self.path_row(layout,'Папка результатов',str(ROOT/'results'),True)
        self.catalog_path=ROOT/'data/catalog.json';self.catalog_button=QPushButton('Характеристики и формулы…')
        self.catalog_button.clicked.connect(self.edit_catalog);layout.addWidget(self.catalog_button)
        self.catalog_label=QLabel('');self.catalog_label.setWordWrap(True);layout.addWidget(self.catalog_label)
        self.fields.append(self.catalog_button);self.refresh_catalog()
        buttons=QHBoxLayout();self.prepare=QPushButton('1. Подготовить эксперименты');self.get_data=QPushButton('2. Скачать данные')
        self.start=QPushButton('3. Дозагрузить / рассчитать → Excel');self.check=QPushButton('Проверить наличие');self.stop=QPushButton('Остановить')
        self.prepare.clicked.connect(self.prepare_plan);self.get_data.clicked.connect(self.download_data)
        self.start.clicked.connect(self.run_plan);self.check.clicked.connect(self.check_cache);self.stop.clicked.connect(self.cancel)
        for b in (self.prepare,self.check,self.get_data,self.start,self.stop):buttons.addWidget(b)
        layout.addLayout(buttons);self.bar=QProgressBar();layout.addWidget(self.bar)
        self.log=QPlainTextEdit();self.log.setReadOnly(True);layout.addWidget(self.log,1);self.setCentralWidget(root)
        self.log.setPlainText('Даты включительно: 00:00 первого дня — 23:00 последнего, UTC.\n'
            'Архив AWS сохраняется на диск; расчёт читает его без интернета.\n'
            'Для накопления архива выберите весь пул. Для небольшой загрузки снимите флажок и подготовьте эксперименты.\n'
            'Расчёт автоматически дозагружает недостающее только для выбранных ячеек и расчётных дат.')
        self.busy(False)

    def date_row(self,form,label,key,start,end):
        row=QHBoxLayout();widgets=[]
        for name,default in [('from',start),('to',end)]:
            w=QDateEdit();w.setCalendarPopup(True);w.setDisplayFormat('yyyy.MM.dd')
            w.setMinimumDate(QDate(2001,1,1));last=datetime.now(timezone.utc).date()-timedelta(days=1)
            w.setMaximumDate(QDate(last.year,last.month,last.day));value=QDate.fromString(str(self.settings.value(key+'_'+name,default)),'yyyy-MM-dd')
            w.setDate(value if value.isValid() else QDate.fromString(default,'yyyy-MM-dd'))
            self.fields.append(w);row.addWidget(w);widgets.append(w)
        form.addRow(label,row);return widgets
    def interval(self,kind):
        a,b=(self.download_from,self.download_to) if kind=='download' else (self.calc_from,self.calc_to)
        return dates(a.date().toString('yyyy-MM-dd'),b.date().toString('yyyy-MM-dd'))
    def save_settings(self):
        for key,w in [('Пул (один раз)',self.pool_path),('Папка кэша',self.cache),('Папка результатов',self.output)]:self.settings.setValue(key,w.text())
        for key,widgets in [('download',(self.download_from,self.download_to)),('calculate',(self.calc_from,self.calc_to))]:
            for name,w in zip(('from','to'),widgets):self.settings.setValue(key+'_'+name,w.date().toString('yyyy-MM-dd'))
    def refresh_catalog(self):
        from .catalog import load_catalog
        data=load_catalog(self.catalog_path);names=[e['name'] for k in ('formulas','curves') for e in data[k] if e.get('enabled')]
        drafts=sum(not e.get('enabled') for k in ('formulas','curves') for e in data[k])
        self.catalog_label.setText('В расчёте: '+', '.join(names)+f'. Не включено: {drafts}.')
    def edit_catalog(self):
        from .catalog_ui import CatalogDialog
        CatalogDialog(self.catalog_path,self).exec();self.refresh_catalog()
    def spin(self,lo,hi,value):
        w=QSpinBox();w.setRange(lo,hi);w.setValue(value);w.valueChanged.connect(self.invalidate);self.fields.append(w);return w
    def path_row(self,layout,label,default,directory):
        row=QHBoxLayout();row.addWidget(QLabel(label));w=QLineEdit(str(self.settings.value(label,default)));b=QPushButton('Выбрать…')
        def choose():
            path=QFileDialog.getExistingDirectory(self,label,w.text()) if directory else QFileDialog.getOpenFileName(self,label,w.text(),'Пул (*.sqlite)')[0]
            if path:w.setText(path)
        b.clicked.connect(choose)
        if not directory:w.textChanged.connect(self.invalidate)
        row.addWidget(w,1);row.addWidget(b);layout.addLayout(row);self.fields.extend([w,b]);return w
    def invalidate(self,*args):
        self.plan=None;self.update_buttons()
    def update_buttons(self):
        if not hasattr(self,'start'):return
        running=getattr(self,'is_busy',False)
        self.start.setEnabled(not running and self.plan is not None)
        self.get_data.setEnabled(not running and (self.all_pool.isChecked() or self.plan is not None))
    def busy(self,value):
        self.is_busy=value
        for w in self.fields:w.setEnabled(not value)
        self.prepare.setEnabled(not value);self.check.setEnabled(not value);self.stop.setEnabled(value);self.update_buttons()
    def launch(self,operation,callback):
        self.save_settings();self.busy(True);self.bar.setRange(0,0);self.task=Task(operation)
        self.task.progress.connect(self.progress);self.task.result.connect(callback)
        self.task.failed.connect(lambda s:self.log.appendPlainText('Ошибка: '+s))
        self.task.finished.connect(lambda:self.busy(False));self.task.start()
    def progress(self,done,total,message):
        self.bar.setMaximum(max(1,total));self.bar.setValue(done);self.statusBar().showMessage(message)
        if 'Не удалось обновить журнал' in message or 'Сетевой сбой:' in message or 'уменьшаем интервал' in message or 'Пауза NASA' in message or 'Дозагрузка' in message:self.log.appendPlainText(message)
    def prepare_plan(self):
        n,r,seed=self.n.value(),self.r.value(),self.seed.value();path=self.pool_path.text()
        def done(plan):
            self.plan=plan;u=len(plan['cells']);p=plan['pool_size']
            self.log.appendPlainText(f'\n{r} × {n} = {r*n} включений. Уникальных: {u} из {p} ({u/p:.2%}). Не использовано: {p-u}.')
        self.launch(lambda cancel,progress:make_plan(load(path),n,r,seed,cancel,progress),done)
    def check_cache(self):
        try:da,db=self.interval('download');ca,cb=self.interval('calculate')
        except ValueError as error:QMessageBox.warning(self,'Даты',str(error));return
        path,folder,plan,all_pool=self.pool_path.text(),self.cache.text(),self.plan,self.all_pool.isChecked()
        def operation(cancel,progress):
            cells=load(path).cells if all_pool else (plan['cells'] if plan else [])
            lines=[]
            if cells:
                tasks,ready=inventory(cells,da,db,folder,include_archive=True)
                lines.append(f'Загрузка {da} — {db}: {ready}/{len(cells)} ячеек покрыто ответами; недостающих интервалов до года (не число AWS-блоков): {len(tasks)}.')
            if plan:
                tasks,ready=inventory(plan['cells'],ca,cb,folder,include_archive=True)
                lines.append(f'Расчёт {ca} — {cb}: {ready}/{len(plan["cells"])} ячеек покрыто; недостающих блоков: {len(tasks)}.')
            return '\n'.join(lines or ['Подготовьте эксперименты или выберите весь пул.'])+'\nНаличие ответов ещё не гарантирует качество каждого часа.'
        self.launch(operation,self.log.appendPlainText)
    def download_data(self):
        try:a,b=self.interval('download')
        except ValueError as error:QMessageBox.warning(self,'Даты',str(error));return
        path,folder,all_pool,plan,workers=self.pool_path.text(),self.cache.text(),self.all_pool.isChecked(),self.plan,self.workers.value()
        archive_mode=self.download_mode.currentIndex()==0
        def operation(cancel,progress):
            cells=load(path).cells if all_pool else plan['cells']
            if archive_mode:
                from .cloud_archive import download as download_archive
                return download_archive(cells,a,b,folder,cancel,progress,workers)
            return download(cells,a,b,folder,cancel,progress,workers)
        def done(result):
            self.log.appendPlainText(f"\nЗагрузка: {result['status']}. Сохранено блоков: {result['completed_blocks']}.\nЖурнал: {result['report_path']}")
            for warning in result.get('warnings',[]):self.log.appendPlainText(warning)
            if not result.get('journal_saved',True):self.log.appendPlainText('Последнее состояние журнала не сохранено. Скачанные блоки учтены в индексе кэша; повторный запуск проверит их и продолжит загрузку.')
            for error in result['errors'][:20]:self.log.appendPlainText(str(error))
            if result['quality_gaps']:self.log.appendPlainText(f"Ответов с пропусками: {len(result['quality_gaps'])}. Подробности в журнале; расчёт проверит нужные часы.")
            self.log.appendPlainText('Расчёт запускается отдельно кнопкой 3. Полнота нужных часов проверяется перед вычислениями.')
        self.launch(operation,done)
    def run_plan(self):
        try:
            from .catalog import load_catalog,enabled_models
            a,b=self.interval('calculate');models=enabled_models(load_catalog(self.catalog_path))
            if not models:raise ValueError('В каталоге нет включённых моделей')
        except Exception as error:QMessageBox.warning(self,'Параметры',str(error));return
        plan,height,folder=self.plan,self.height.value(),self.cache.text()
        workers=self.workers.value()
        output=Path(self.output.text())/datetime.now().strftime('run_%Y%m%d_%H%M%S_%f')
        def operation(cancel,progress):
            result=calculate(plan,a,b,height,models,folder,output,cancel,progress,workers)
            export_report(plan,result,output/'report.xlsx');return result,output
        def done(value):
            result,out=value
            self.log.appendPlainText(f"\nСтатус: {result['status']}. Рассчитано ячеек: {len(result['cells'])}.\nExcel: {out/'report.xlsx'}")
            if result['errors']:self.log.appendPlainText('\n'.join(result['errors'][:30]))
            if result['status']!='complete':self.log.appendPlainText('Итоговые выборки не сформированы. Полный список причин — в result.json и Excel. Необходимые данные получить не удалось. Кэш сохранён; повторите расчёт позднее.')
        self.launch(operation,done)
    def cancel(self):
        if self.task:self.task.cancel.set();self.stop.setEnabled(False);self.log.appendPlainText('Остановка после текущих запросов (сетевой тайм-аут 30 с)…')
    def closeEvent(self,event):
        if self.task and self.task.isRunning():self.cancel();event.ignore()
        else:event.accept()

def run():
    app=QApplication.instance() or QApplication([]);window=Window();window.show();return app.exec()
