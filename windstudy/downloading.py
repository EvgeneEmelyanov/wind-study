"""Загрузка по годам, ограниченная параллельность, адаптивное дробление."""
from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED,CancelledError
from collections import deque
from datetime import datetime,timezone,timedelta
from pathlib import Path
import json
from urllib.error import HTTPError
from .meteo import fetch,Cancelled
from .weather_store import inventory,db_open,url_for,put


def download(cells,start,end,folder,cancel,progress,workers=1,downloader=None):
    from .request_gate import RequestGate
    gate=RequestGate(workers)
    if downloader is None:
        downloader=lambda url,cancel,on_retry:fetch(url,cancel,on_retry,gate=gate)
    tasks,ready=inventory(cells,start,end,folder)
    report={'start':str(start),'end':str(end),'cells':len(cells),'already_present':ready,'planned_requests':len(tasks),
            'status':'running','completed_blocks':0,'errors':[],'quality_gaps':[],'workers':workers}
    folder=Path(folder);logpath=folder/('download_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')+'.json')
    def save():
        tmp=logpath.with_suffix('.tmp');tmp.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');tmp.replace(logpath)
    db=db_open(folder);queue=deque(tasks);pending={};done=0
    def request(task):
        c,a,b=task
        return downloader(url_for(c,a,b),cancel,on_retry=lambda m:progress(done,max(len(tasks),1),m))
    workers=max(1,min(5,workers))
    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            def fill():
                while len(pending)<workers and not cancel.is_set():
                    task=queue.popleft() if queue else None
                    if task is None:return
                    pending[executor.submit(request,task)]=task
            fill()
            while pending:
                completed,_=wait(pending,timeout=.2,return_when=FIRST_COMPLETED)
                for future in completed:
                    c,a,b=pending.pop(future)
                    try:
                        raw=future.result()
                    except (Cancelled,CancelledError):pass
                    except Exception as error:
                        # Дробление после исчерпания повторов, до интервалов около месяца.
                        http=isinstance(error,HTTPError)
                        rate_limited=isinstance(error.__cause__,HTTPError) and error.__cause__.code==429
                        splittable=not rate_limited and isinstance(error,(ConnectionError,TimeoutError,OSError)) and (not http or error.code in (408,413,422,500,502,503,504))
                        if splittable and (b-a).days>31 and not cancel.is_set():
                            mid=a+(b-a)//2
                            for x,y in ((a,mid),(mid+timedelta(days=1),b)):
                                task=(c,x,y);tasks.append(task);queue.append(task)
                            progress(done,len(tasks),f'{c[0]}: уменьшаем интервал после сбоя')
                        else:
                            report['errors'].append({'cell':c[0],'start':str(a),'end':str(b),'error':str(error)})
                            if rate_limited:queue.clear()
                    else:
                        # Ошибки диска не маскируются под сетевой сбой и не дробят запрос.
                        try:
                            valid,total=put(db,c,a,b,raw)
                            report['completed_blocks']+=1
                            if valid<total:report['quality_gaps'].append({'cell':c[0],'start':str(a),'end':str(b),'valid_both':valid,'expected':total})
                        except ValueError as error:
                            report['errors'].append({'cell':c[0],'start':str(a),'end':str(b),'error':str(error)})
                    done+=1;progress(done,max(len(tasks),1),f'Загрузка: {done}/{len(tasks)} блоков; {c[0]} {a} — {b}')
                    save()
                if cancel.is_set():
                    for future in pending:future.cancel()
                else:fill()
            report['status']='cancelled' if cancel.is_set() else ('incomplete' if report['errors'] or report['quality_gaps'] else 'complete')
    except Exception:
        report['status']='incomplete'
        cancel.set()
        raise
    finally:db.close();save()
    report['report_path']=str(logpath);return report
