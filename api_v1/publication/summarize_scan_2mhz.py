"""Opt-in capture decoder for the 2 MHz calibration pilot; no hardware changes.

Reject incomplete frames and irregular clock periods. Record unfiltered decode
and filter disagreement rather than guessing a packet from the requested cell.
"""
import json
from pathlib import Path
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import summarize_capture as base

def decode_filtered(path, width):
    channels = {c: base.deglitch(base.transitions_from_csv(path, c), width if c == 8 else 80e-9)
                for c in (8, 9, 10, 11)}
    clk, tm, dl, dr = (channels[c] for c in (8, 9, 10, 11))
    rise = base.first_transition(tm, 0, 1)
    if rise is None:
        raise ValueError('Missing TM rise: not a scan capture')
    fall = base.first_transition(tm, 1, 0, after=rise)
    drfall = base.first_transition(dr, 1, 0, after=rise)
    drrise = base.first_transition(dr, 0, 1, after=drfall) if drfall is not None else None
    if None in (fall, drfall, drrise) or not rise < drfall < drrise < fall:
        raise ValueError('Incomplete or out-of-order scan frame')
    edges = [t for (pt, pv), (t, v) in zip(clk, clk[1:])
             if pv == 0 and v == 1 and drfall < t < drrise]
    periods = [b-a for a,b in zip(edges, edges[1:])]
    if len(edges) != 18 or not all(0.44e-6 <= p <= 0.56e-6 for p in periods):
        raise ValueError(f'Invalid 2 MHz clock/frame: {len(edges)} samples, periods={periods}')
    bits = [base.value_at(dl,t) for t in edges]
    packet = sum(bits[1+i] << i for i in range(16))
    return dict(decoded_packet=packet, decoded_packet_hex=f'0x{packet:04x}',
                low_sample_count=len(bits), low_samples=''.join(map(str,bits)),
                tm_rise_s=rise, tm_fall_s=fall, dr_fall_s=drfall, dr_rise_s=drrise,
                dr_low_width_s=drrise-drfall, clock_period_s_median=statistics.median(periods))

raw_decode = base.decode_packet

def decode_packet(path):
    raw = raw_decode(path)
    alternatives = [decode_filtered(path, w) for w in (40e-9,80e-9,100e-9)]
    if len({x['low_samples'] for x in alternatives}) != 1:
        raise ValueError('Clock filter thresholds disagree; capture rejected')
    result = alternatives[0]
    result['unfiltered_decode'] = raw
    result['clock_filter_ns'] = [40,80,100]
    Path(path).with_name('clock_decode_diagnostic.json').write_text(json.dumps(result,indent=2))
    return result

if __name__ == '__main__':
    base.decode_packet = decode_packet
    raise SystemExit(base.main())
