"""Bounded SET/RESET acquisition for empirical TDC fine-count calibration.

Three column-31 cells are bracketed by paired scan reads of columns 30/31.
Every state gets two scan repeats and one independent WB transaction. Cell
programming is finite: at most three SET and four RESET pulses per cell.
"""
import csv
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import statistics
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cell_api import CommandRunner, ScanDebugCellAPI, ScanDebugConfig, decode_wb_return

ROWS=(4,6,13)
SET_LEVELS=((2.4,1.56),(2.4,1.75),(2.4,1.99))
RESET_LEVELS=((2.3,1.19),(2.3,1.44),(2.3,1.69),(2.3,1.94))
SET_LIMIT_US=65.0
RESET_LIMIT_US=8.0

class BenchTransport(CommandRunner):
    def run(self,cmd,**kwargs):
        if (Path(cmd[0]).name.lower() in ('scp','scp.exe') and '-O' not in cmd
                and any('geethika@100.116.216.70:' in str(a) for a in cmd)):
            cmd=[cmd[0],'-O',*cmd[1:]]
        return super().run(cmd,**kwargs)

def packet(mode,row,col=31):
    mode_bits={'reset':0,'read':1,'set':3}[mode]
    return (mode_bits<<30)|(row<<25)|(col<<20)|(2<<16)|0xAAFF

def direct_dacs(api,specs):
    """Apply one or more ordered DAC payloads through one guarded SSH call."""
    if len(specs)>2:
        applied=[]
        for start in range(0,len(specs),2):
            applied.extend(direct_dacs(api,specs[start:start+2]))
        return applied
    commands=[f"Set-Location '{api.config.zynq_dir}'"]
    items=[]
    for channel,voltage,signal,span in specs:
        payload=api._runtime_direct_dac_payload(channel,voltage,span)
        request_id='finecal_'+uuid.uuid4().hex
        response=f'runtime_vio_response.{request_id}.txt'
        temporary=f'runtime_vio_request.{request_id}.tmp'
        commands.extend([
            f"$id='{request_id}'",
            f"$tmp='{temporary}'",
            f"$resp='{response}'",
            f"Set-Content -NoNewline -LiteralPath $tmp -Value \"$id {payload}\"",
            "Move-Item -Force -LiteralPath $tmp -Destination runtime_vio_request.txt",
            "$deadline=(Get-Date).AddSeconds(20)",
            "while(-not (Test-Path -LiteralPath $resp) -and (Get-Date) -lt $deadline){ Start-Sleep -Milliseconds 50 }",
            "if(-not (Test-Path -LiteralPath $resp)){ throw \"DAC runtime response timeout for $id\" }",
            "$reply=Get-Content -LiteralPath $resp -Raw",
            "Remove-Item -LiteralPath $resp -Force",
            "if($reply -notmatch ([regex]::Escape($id)+' OK ')){ throw \"DAC runtime rejected: $reply\" }",
            "Write-Output $reply",
        ])
        items.append({'signal':signal,'dac_channel':channel,'voltage_v':voltage,'span_v':span,
            'dac_code':f"0x{api._dac_code(voltage,span):04X}",'runtime_command':payload,'ok':True})
    result=api._run_zynq_powershell('\n'.join(commands),timeout_s=max(60,25*len(items)))
    if result.returncode!=0:
        raise RuntimeError(result.stdout or 'batched DAC update failed')
    for item in items:
        api._append_jsonl('fine_cal_dac_updates.jsonl',item)
        print('DAC='+json.dumps(item),flush=True)
    return items

def direct_dac(api,channel,voltage,signal,span=5.0):
    return direct_dacs(api,[(channel,voltage,signal,span)])[0]

def scan_once(api,row,col):
    result=asdict(api.read(row,col))
    capture=Path(result['local_output_dir'])
    summary=json.loads((capture/'capture_summary.json').read_text())
    timing=summary['decoded']
    v=[];wl=[]
    with (capture/'analog.csv').open(newline='') as handle:
        for rec in csv.DictReader(handle):
            t=float(rec['Time [s]'])
            if timing['dr_rise_s']<=t<=timing['tm_fall_s']:
                v.append(float(rec['Channel 0']));wl.append(float(rec['Channel 1']))
    result.update(raw_current_uA=summary['la_set_window_mean_uA'],read_V=statistics.mean(v),
                  wl_V=statistics.mean(wl),G_uS=result['current_uA']/statistics.mean(v),
                  hold_us=(timing['tm_fall_s']-timing['dr_rise_s'])*1e6)
    return result

def scans_for_state(api,row,state_index):
    repeats=[]
    for repeat in range(2):
        values={}
        order=(30,31) if (state_index+repeat)%2==0 else (31,30)
        for col in order:
            print(f'SCAN r{row}c{col} state={state_index} repeat={repeat+1}',flush=True)
            values[str(col)]=scan_once(api,row,col)
        values['delta_uS']=values['31']['G_uS']-values['30']['G_uS']
        repeats.append(values)
    return repeats

def wb_read(api,row):
    value=packet('read',row)
    result=api.wishbone_access('read',value)
    frames=[dict(raw=w,**decode_wb_return(int(w,16))) for w in result.get('readbacks',[]) if int(w,16)]
    matching=[x for x in frames if x['extra']==0 and x['col_addr']==31]
    if not result.get('ok') or not matching:
        raise RuntimeError(f'No valid column-31 WB response for row {row}')
    result['matching_frames']=matching
    result['selected_matching']=matching[0]
    return result

def observe(api,row,state_index,phase,rails,pulse_result=None):
    scans=scans_for_state(api,row,state_index)
    wb=wb_read(api,row)
    item={'row':row,'state_index':state_index,'phase':phase,'rails':rails,
          'pulse_result':pulse_result,'scan_repeats':scans,
          'G30_mean_uS':statistics.mean(x['30']['G_uS'] for x in scans),
          'G31_mean_uS':statistics.mean(x['31']['G_uS'] for x in scans),
          'delta_mean_uS':statistics.mean(x['delta_uS'] for x in scans),
          'delta_range_uS':max(x['delta_uS'] for x in scans)-min(x['delta_uS'] for x in scans),
          'wb':wb,'selected':wb['selected_matching'],'timestamp':datetime.now().isoformat()}
    print('STATE='+json.dumps({k:item[k] for k in ('row','state_index','phase','G30_mean_uS','G31_mean_uS','delta_mean_uS','delta_range_uS','selected')}),flush=True)
    return item

def program(api,row,operation,vcc,wl):
    if operation=='set':
        updates=direct_dacs(api,[(2,vcc,'vcc_set',5.0),(3,wl,'vcc_wl_set',5.0)])
    else:
        updates=direct_dacs(api,[(4,wl,'vcc_wl_reset',5.0),(5,vcc,'vcc_reset',5.0)])
    value=packet(operation,row)
    result=api.wishbone_access('write',value)
    if not result.get('ok'):
        raise RuntimeError(f'{operation} command failed for row {row}')
    return {'operation':operation,'packet':f'0x{value:08X}','rail_updates':updates,'wb':result}

def main():
    root=Path(__file__).resolve().parents[1]
    run=root/'runs'/('fine_count_calibration_'+datetime.now().strftime('%Y%m%d_%H%M%S'))
    config=ScanDebugConfig(run_dir=run,attempts=2,read_feedback_attempts=1,
        trigger_channel=9,trigger_edge='rising',summarizer=Path(__file__).with_name('summarize_scan_2mhz.py'),
        read_calibration_path=Path(__file__).with_name('read_offset_fine_calibration_20260928.json'),
        defer_capture_copy=False,hardware_queue_timeout_seconds=15,wishbone_persistent_uart=True,
        wishbone_skip_passive_snapshot=True,wishbone_uart_timeout_seconds=40)
    api=ScanDebugCellAPI(config);api.runner=BenchTransport()
    # The required runtime files were verified present on the Windows FPGA host
    # immediately before this run. Avoid the known OpenSSH/SCP close hang that
    # can occur while redundantly uploading those unchanged files.
    api._ensure_remote_fpga_sources=lambda: None
    # The full v35 BIT/LTX and runtime Tcl files are already verified remotely.
    # Start one fresh daemon owned by this process; interrupted owners terminate
    # their daemon, so attaching to an earlier process would be unsafe.
    api._runtime_bitstream_ready=True
    report={'run_dir':str(run),'rows':ROWS,'set_levels':SET_LEVELS,'reset_levels':RESET_LEVELS,
            'set_limit_uS':SET_LIMIT_US,'reset_limit_uS':RESET_LIMIT_US,'iref_V':1.0,
            'quantity':'G31-G30','max_set_pulses_per_cell':len(SET_LEVELS),
            'max_reset_pulses_per_cell':len(RESET_LEVELS),'states':[],'complete':False}
    report['remote_fpga_sources']='preflight-verified; redundant SCP upload skipped'
    report['runtime_daemon']='fresh process-owned v35 daemon; redundant source upload skipped'
    def save():
        run.mkdir(parents=True,exist_ok=True)
        (run/'fine_count_calibration.json').write_text(json.dumps(report,indent=2,default=str))
        fields=['row','state_index','phase','vcc_V','wl_V','G30_mean_uS','G31_mean_uS','delta_mean_uS','delta_range_uS','wb_raw','coarse','fine','fit_eligible']
        with (run/'fine_count_calibration.csv').open('w',newline='') as handle:
            writer=csv.DictWriter(handle,fieldnames=fields);writer.writeheader()
            for x in report['states']:
                selected=x['selected'];rails=x['rails'] or {}
                writer.writerow({'row':x['row'],'state_index':x['state_index'],'phase':x['phase'],
                    'vcc_V':rails.get('vcc_V',''),'wl_V':rails.get('wl_V',''),
                    'G30_mean_uS':x['G30_mean_uS'],'G31_mean_uS':x['G31_mean_uS'],
                    'delta_mean_uS':x['delta_mean_uS'],'delta_range_uS':x['delta_range_uS'],
                    'wb_raw':selected['raw'],'coarse':selected['coarse_cnt'],'fine':selected['fine_cnt'],
                    'fit_eligible':0<selected['coarse_cnt']<51})
    print('RUN_DIR='+str(run),flush=True);save()
    try:
        with api.hardware_queue('bounded-fine-count-calibration'):
            api._ensure_runtime_vio_daemon()
            # Restore all fixed baseline DACs before any cell operation.
            direct_dacs(api,[(9,1.0,'iref',5.0),(10,.9,'vcomp',10.0),(11,.6,'bias_comp2',5.0),
                (12,1.6,'vbias',5.0),(13,1.5,'dc_bias',5.0),(7,4.0,'VDDIO',5.0),
                (0,.5,'vcc_read',10.0),(1,2.5,'vcc_wl_read',5.0)])
            for row in ROWS:
                state_index=0
                item=observe(api,row,state_index,'baseline',None);report['states'].append(item);save()
                # First raise the cell through a few proven SET rails.
                for vcc,wl in SET_LEVELS:
                    if item['G31_mean_uS']>=SET_LIMIT_US: break
                    pulse=program(api,row,'set',vcc,wl);state_index+=1
                    item=observe(api,row,state_index,'after-set',{'vcc_V':vcc,'wl_V':wl},pulse)
                    report['states'].append(item);save()
                # Then traverse downward to supply fine-count points in the opposite direction.
                for vcc,wl in RESET_LEVELS:
                    if item['G31_mean_uS']<=RESET_LIMIT_US: break
                    pulse=program(api,row,'reset',vcc,wl);state_index+=1
                    item=observe(api,row,state_index,'after-reset',{'vcc_V':vcc,'wl_V':wl},pulse)
                    report['states'].append(item);save()
        report['complete']=True
    except Exception as exc:
        report['error']=f'{type(exc).__name__}: {exc}'
        print('CALIBRATION_STOP='+report['error'],flush=True)
    finally: save()
    print('CALIBRATION_RESULT='+str(run/'fine_count_calibration.json'),flush=True)
    return 0 if report['complete'] else 1

if __name__=='__main__': raise SystemExit(main())
