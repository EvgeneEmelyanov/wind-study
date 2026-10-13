"""Общий лимит запросов одной загрузки; 429 приостанавливает всю очередь."""
import random
import time
from threading import Lock
from .meteo import Cancelled

class RequestGate:
    def __init__(self,limit=1,spacing=2.0,clock=time.monotonic):
        self.limit=max(1,min(5,limit));self.spacing=spacing;self.clock=clock
        self.lock=Lock();self.active=0;self.next_start=0.;self.cooldown=0.;self.strikes=0
        self.next_notice=0.

    def acquire(self,cancel,notify):
        while True:
            if cancel.is_set():raise Cancelled()
            message=None
            with self.lock:
                now=self.clock();deadline=max(self.cooldown,self.next_start)
                if self.active<self.limit and now>=deadline:
                    self.active+=1;self.next_start=now+self.spacing;return
                if self.cooldown>now and now>=self.next_notice:
                    message=f'Пауза NASA 429: до следующей попытки {int(self.cooldown-now)+1} с; параллельность 1'
                    self.next_notice=now+5
                delay=min(.25,max(.01,deadline-now)) if deadline>now else .25
            if message:notify(message)
            if cancel.wait(delay):raise Cancelled()

    def release(self):
        with self.lock:self.active-=1

    def penalize(self,retry_after=0):
        with self.lock:
            now=self.clock();self.limit=1
            # Несколько ответов от уже отправленных запросов — одна волна отказов.
            if now>=self.cooldown:self.strikes+=1
            delay=max(retry_after,min(900,60*2**min(self.strikes-1,4)))+random.uniform(1,5)
            self.cooldown=max(self.cooldown,now+delay);self.next_notice=0.
            return self.cooldown-now
