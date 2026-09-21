"""One-shot, isolated comparison of three ASR delivery modes on identical PCM."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import signal
import threading
import time
import wave
import uuid

import httpx
from app.providers import Feishu, Company, ProviderError
from app.config import Settings
from app.store import Store
from app.security import sign
from app.realtime import StreamRecognizer, StreamRateLimit
from app.recording_live import growing_pcm, persist_pcm

MODES=('realtime','short_file','batch_file','company_batch')


def summarize(events, started, duration):
    report={}
    for mode in MODES:
        items=[e for e in events if e['mode']==mode]
        results=[e for e in items if e['kind']=='result']
        good=[e for e in results if e['ok']]
        texts=[e['at'] for e in items if e.get('text') and (e['kind']=='partial' or e.get('ok'))]
        successful=sum(e['duration'] for e in good)
        beginnings=[e['at'] for e in items if e['kind']=='started']
        processing_start=min(beginnings) if beginnings else None
        report[mode]={'first_text_seconds':round(min(texts)-started,2) if texts and started else None,
            'first_final_seconds':round(min(e['at'] for e in good)-started,2) if good and started else None,
            'last_result_seconds':round(max(e['at'] for e in results)-started,2) if results and started else None,
            'successful_audio_seconds':round(successful,3),'coverage_ratio':round(successful/duration,4) if duration else 0,
            'failed_segments':sum(not e['ok'] for e in results),'retries':sum(e['kind']=='retry' for e in items),
            'api_requests':sum(e['kind']=='request' for e in items),
            'rate_limit_responses':sum(e['kind']=='request' and e.get('rate_limited',False) for e in items),
            'text_characters':sum(len(e['text']) for e in good),
            'processing_seconds':round(max(e['at'] for e in results)-processing_start,2) if results and processing_start else None}
    return report


class Benchmark:
    def __init__(self, root, seconds=1800, wait_seconds=7200, resume_waiting=False):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        if (self.root/'progress.json').exists():
            prior=json.loads((self.root/'progress.json').read_text())
            if not resume_waiting or prior.get('started_at') is not None or prior.get('captured_seconds',0)!=0:
                raise ValueError('Use a fresh benchmark directory; only an unstarted wait may resume')
        self.seconds=seconds;self.wait_seconds=wait_seconds;self.armed=time.time();self.started=None
        self.duration=0;self.phase='waiting_for_stream';self.events=[];self.lock=threading.RLock()
        self.stop=threading.Event();self.capture_stop=threading.Event();self.captured=threading.Event()
        self.stream_queue=queue.Queue();self.file_queue=queue.Queue();self.files=[];self.errors=[]
        self.done=set();self.completed_capture_at=None

    def emit(self,mode,kind,**values):
        item=dict(mode=mode,kind=kind,at=time.time(),**values)
        with self.lock:
            self.events.append(item)
            with (self.root/'events.jsonl').open('a') as f:f.write(json.dumps(item,ensure_ascii=False)+'\n')
            if kind=='result' and values.get('ok'):
                with (self.root/(mode+'.txt')).open('a') as f:
                    f.write(f"[{values['offset']:.2f}–{values['offset']+values['duration']:.2f}s]\n{values['text']}\n\n")

    def progress(self):
        with self.lock:
            data={'phase':self.phase,'armed_at':self.armed,'started_at':self.started,'captured_seconds':round(self.duration,3),
                'target_seconds':self.seconds,'updated_at':time.time(),'completed_modes':sorted(self.done),'errors':list(self.errors),
                'capture_complete':self.duration>=self.seconds-.01,'capture_ended_at':self.completed_capture_at,
                'modes':summarize(self.events,self.started,self.duration),
                'caveat':'Same Feishu app; simultaneous modes may share rate limits. Text length is not accuracy.'}
            temp=self.root/'progress.tmp';temp.write_text(json.dumps(data,ensure_ascii=False,indent=2));temp.replace(self.root/'progress.json')

    def provider(self,mode):
        def response(r):
            if '/speech/' not in r.request.url.path and '/asr/v1/' not in r.request.url.path:return
            r.read()
            try:code=r.json().get('code')
            except ValueError:code=None
            self.emit(mode,'request',status=r.status_code,code=code,rate_limited=r.status_code==429 or code in (10024,99991400))
        client=httpx.Client(timeout=httpx.Timeout(60,connect=10),event_hooks={'response':[response]})
        if mode=='company_batch':
            c=Settings()
            return Company(c.company_url,c.company_host,c.company_uid,c.hotwords,client)
        p=Feishu(os.environ['FEISHU_APP_ID'],os.environ['FEISHU_APP_SECRET'],client)
        p.stream_min_interval=1.05
        return p

    def file_one(self,provider,mode,item):
        path,offset,duration=item
        for attempt in range(3):
            if self.stop.is_set():return
            try:
                result=provider.transcribe(mode+'-'+path.stem,path)
                self.emit(mode,'result',offset=offset,duration=duration,ok=True,text=result['text'])
                return
            except Exception as exc:
                error=str(exc) if isinstance(exc,ProviderError) else type(exc).__name__
                retry=isinstance(exc,(httpx.TransportError,TimeoutError)) or isinstance(exc,ProviderError) and exc.retryable
                if retry and attempt<2:
                    self.emit(mode,'retry',offset=offset,error=error)
                    if self.stop.wait(10*(attempt+1)):return
                else:
                    self.emit(mode,'result',offset=offset,duration=duration,ok=False,text='',error=error)
                    return

    def files_worker(self,mode):
        p=self.provider(mode)
        try:
            if mode=='batch_file':
                while not self.captured.wait(.5):
                    if self.stop.is_set():return
                self.emit(mode,'started')
                for item in self.files:
                    self.file_one(p,mode,item)
                    if self.stop.is_set():break
            else:
                began=False
                while not self.stop.is_set():
                    try:item=self.file_queue.get(timeout=.5)
                    except queue.Empty:continue
                    if item is None:break
                    if not began:self.emit(mode,'started');began=True
                    self.file_one(p,mode,item)
        except Exception as exc:
            with self.lock:self.errors.append(mode+': '+type(exc).__name__)
        finally:
            p.client.close()
            with self.lock:self.done.add(mode)

    def stream_worker(self):
        p=self.provider('realtime');session=None;offset=0.;length=0.;failure=None;cooldown=0.
        try:
            began=False
            while not self.stop.is_set():
                try:packet=self.stream_queue.get(timeout=.5)
                except queue.Empty:continue
                ending=packet is None
                if ending and not length:break
                if not ending:
                    if not began:self.emit('realtime','started');began=True
                    if not length:
                        session=StreamRecognizer(p);failure=None
                    length+=len(packet)/32000
                    if failure is None:
                        try:
                            if time.time()<cooldown:raise ProviderError('Streaming cooldown; audio retained in benchmark WAV')
                            text=session.send(packet)
                            if text:self.emit('realtime','partial',offset=offset,text=text)
                        except Exception as exc:
                            failure=str(exc) if isinstance(exc,ProviderError) else type(exc).__name__
                            if isinstance(exc,StreamRateLimit):cooldown=time.time()+exc.retry_after
                            elif session.attempted_sequence>=0:session.abort()
                if length>=5 or ending:
                    text=''
                    if failure is None:
                        try:text=session.finish()
                        except Exception as exc:
                            failure=str(exc) if isinstance(exc,ProviderError) else type(exc).__name__
                            if isinstance(exc,StreamRateLimit):cooldown=time.time()+exc.retry_after
                            elif session.attempted_sequence>=0:session.abort()
                    self.emit('realtime','result',offset=offset,duration=length,ok=failure is None,text=text,error=failure or '')
                    offset+=length;length=0;session=None
                if ending:break
        except Exception as exc:
            with self.lock:self.errors.append('realtime: '+type(exc).__name__)
        finally:
            if session is not None:session.abort()
            p.client.close()
            with self.lock:self.done.add('realtime')

    def company_worker(self):
        mode='company_batch';p=self.provider(mode)
        try:
            while not self.captured.wait(.5):
                if self.stop.is_set():return
            if not self.duration:return
            self.emit(mode,'started')
            cfg=Settings();db=Store(cfg.data/'state.sqlite')
            job_id='benchmark-'+uuid.uuid4().hex;task_id=str(uuid.uuid4())
            audio=(self.root/'capture.wav').resolve().relative_to(cfg.data.resolve())
            # A dedicated non-runnable row only serves signed audio to the company ASR.
            # No normal worker or roster export processes this room/state.
            with db.connect() as conn:
                conn.execute("INSERT INTO jobs(id,room,path,start,duration,provider,remote_id,created,state) VALUES(?,?,?,?,?,?,?,?,?)",
                    (job_id,'live/asr-benchmark',str(audio),self.started,self.duration,'company',task_id,time.time(),'benchmark'))
            expiry=int(time.time())+7200
            url=cfg.public_url+'/audio/'+job_id+'.wav?expires='+str(expiry)+'&signature='+sign(cfg.signing_key,job_id,expiry)
            (self.root/'company-task.json').write_text(json.dumps({'job_id':job_id,'task_id':task_id}))
            for attempt in range(3):
                try:p.submit(task_id,url);break
                except Exception as exc:
                    if attempt==2:raise
                    self.emit(mode,'retry',error=str(exc) if isinstance(exc,ProviderError) else type(exc).__name__)
                    if self.stop.wait(10):return
            while not self.stop.is_set():
                try:
                    result=p.poll(task_id)
                    if result is not None:
                        self.emit(mode,'result',offset=0,duration=self.duration,ok=True,text=result['text'])
                        (self.root/'company-result.json').write_text(json.dumps(result,ensure_ascii=False))
                        return
                except Exception as exc:
                    if not (isinstance(exc,httpx.TransportError) or isinstance(exc,ProviderError) and exc.retryable):raise
                    self.emit(mode,'retry',error=str(exc) if isinstance(exc,ProviderError) else type(exc).__name__)
                self.stop.wait(5)
        except Exception as exc:
            self.emit(mode,'result',offset=0,duration=self.duration,ok=False,text='',error=str(exc) if isinstance(exc,ProviderError) else type(exc).__name__)
        finally:
            p.client.close()
            with self.lock:self.done.add(mode)

    def capture(self,recordings,room):
        source=None;deadline=time.time()+self.wait_seconds
        while not self.stop.is_set() and time.time()<deadline:
            for file in sorted((Path(recordings)/room).glob('*.mp4')):
                try:started=int(file.stem.split('-')[0])+int(file.stem.split('-')[1])/1e6
                except (ValueError,IndexError):continue
                active=not file.with_suffix('.ready.json').exists() and file.stat().st_mtime>=self.armed-2
                if (started>=self.armed-2 or active) and file.stat().st_size>0:
                    source=file;break
            if source:break
            self.stop.wait(.5)
        if source is None:
            self.errors.append('No new stream before wait deadline');return
        self.phase='capturing'
        chunks=bytearray();packet=bytearray();offset=0.;index=0
        source_start=int(source.stem.split('-')[0])+int(source.stem.split('-')[1])/1e6
        skip=round(max(0,self.armed-source_start)*16000)*2
        self.emit('capture','attached',skipped_seconds=skip/32000)
        decoder=growing_pcm(source,source.with_suffix('.ready.json'),self.capture_stop)
        def save_chunk():
            nonlocal index,offset
            if not chunks:return
            path=self.root/'audio'/f'{index:05d}.wav';persist_pcm(path,chunks)
            item=(path,offset,len(chunks)/32000);self.files.append(item);self.file_queue.put(item)
            offset+=len(chunks)/32000;index+=1;chunks.clear()
        try:
            with wave.open(str(self.root/'capture.wav'),'wb') as output:
                output.setparams((1,2,16000,0,'NONE','not compressed'))
                for block in decoder:
                    if self.stop.is_set():break
                    if skip:
                        drop=min(skip,len(block));skip-=drop;block=block[drop:]
                        if not block:continue
                    if self.started is None:self.started=time.time()
                    remaining=round((self.seconds-self.duration)*32000)
                    block=block[:remaining]
                    output.writeframes(block);self.duration+=len(block)/32000
                    chunks.extend(block);packet.extend(block)
                    while len(packet)>=32000:
                        self.stream_queue.put(bytes(packet[:32000]));del packet[:32000]
                    if len(chunks)>=30*32000:save_chunk()
                    if self.duration>=self.seconds-.001:break
                if packet:self.stream_queue.put(bytes(packet))
                save_chunk()
        except Exception as exc:
            self.errors.append('Capture: '+type(exc).__name__)
        finally:
            self.capture_stop.set();decoder.close()
            self.completed_capture_at=time.time()
            if self.duration<self.seconds-.01:self.errors.append('Stream ended before target duration')
            self.phase='processing';self.stream_queue.put(None);self.file_queue.put(None);self.captured.set()

    def run(self,recordings,room):
        self.progress()
        workers=[threading.Thread(target=self.stream_worker),threading.Thread(target=self.files_worker,args=('short_file',)),threading.Thread(target=self.files_worker,args=('batch_file',)),threading.Thread(target=self.company_worker)]
        for t in workers:t.start()
        def collect():
            try:self.capture(recordings,room)
            except Exception as exc:self.errors.append('Capture: '+type(exc).__name__)
            finally:
                self.stream_queue.put(None);self.file_queue.put(None);self.captured.set()
        recorder=threading.Thread(target=collect);recorder.start()
        try:
            while recorder.is_alive() or any(t.is_alive() for t in workers):
                if self.captured.is_set() and self.completed_capture_at and time.time()-self.completed_capture_at>3600:
                    self.errors.append('Processing exceeded one hour after capture');self.stop.set()
                self.progress()
                if self.stop.wait(5):break
        finally:
            self.stop.set();self.capture_stop.set();recorder.join(timeout=20)
            for t in workers:t.join(timeout=70)
            self.phase='finished' if not self.errors and len(self.done)==4 else 'incomplete'
            self.progress()
            if (self.root/'capture.wav').exists():
                digest=hashlib.sha256((self.root/'capture.wav').read_bytes()).hexdigest()
                (self.root/'capture.sha256').write_text(digest+'\n')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True);parser.add_argument('--seconds',type=int,default=1800)
    parser.add_argument('--wait-seconds',type=int,default=7200)
    parser.add_argument('--resume-waiting',action='store_true')
    parser.add_argument('--recordings',default='/data/recordings');parser.add_argument('--room',default='live/taobao')
    a=parser.parse_args()
    if not 1<=a.seconds<=3600:raise SystemExit('seconds must be between 1 and 3600')
    bench=Benchmark(a.output,a.seconds,a.wait_seconds,a.resume_waiting)
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:bench.stop.set())
    bench.run(a.recordings,a.room)


if __name__=='__main__':main()
