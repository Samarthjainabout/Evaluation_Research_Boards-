import csv
from pathlib import Path
import tempfile
import unittest
from summarize_scan_2mhz import decode_filtered

class ScanDecoderTests(unittest.TestCase):
    def frame(self, path, packet=0x03E0, glitch=False, period=0.5e-6, missing_tm=False):
        # Sampled at 50 MHz; stable DL is serialized LSB-first after one dummy.
        bits=[0]+[(packet>>i)&1 for i in range(16)]+[0]
        rows=[]
        previous=None
        for tick in range(-100,3201):
            t=tick*20e-9
            clock=int((t % period) >= period/2)
            if glitch and tick==35:
                clock=1-clock
            tm=int(-.5e-6<=t<59e-6) if not missing_tm else 0
            dr=int(not(0<=t<9e-6))
            bit=min(17,max(0,int((t+1e-12)/.5e-6)))
            dl=bits[bit] if 0<=t<9e-6 else 0
            values=(clock,tm,dl,dr)
            if values!=previous:
                rows.append((t,*values))
                previous=values
        with path.open('w',newline='') as handle:
            writer=csv.writer(handle)
            writer.writerow(['Time [s]']+[f'Channel {i}' for i in (8,9,10,11)])
            writer.writerows(rows)

    def test_frames_and_glitch(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'digital.csv'
            for packet in (0x03E0,0x03C0,0x03E4,0xFFFF,0x0000):
                for glitch in (False,True):
                    self.frame(path,packet,glitch)
                    for width in (40e-9,80e-9,100e-9):
                        self.assertEqual(decode_filtered(path,width)['decoded_packet'],packet)

    def test_reject_missing_tm_and_wrong_clock(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'digital.csv'
            for options in ({'missing_tm':True},{'period':.4e-6}):
                self.frame(path,**options)
                with self.assertRaises(ValueError):
                    decode_filtered(path,80e-9)

if __name__=='__main__':
    unittest.main()
