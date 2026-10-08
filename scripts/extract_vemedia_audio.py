#!/usr/bin/env python3
"""Extract AAC ADTS from a .vemedia snapshot without using the final MP4/moov.

Validated for the supplied double_click_20261008140038.vemedia format only.
Incomplete trailing records are ignored (useful for a still-growing file).
"""
import argparse
from pathlib import Path
import struct


def u32(data, pos):
    return struct.unpack_from('>I', data, pos)[0]


def adts_header(payload_len, obj, freq, channels):
    if obj != 2 or freq == 15 or channels == 0:
        raise ValueError('Only AAC-LC with indexed frequency and explicit channel count supported')
    total = payload_len + 7
    if total >= 8192:
        raise ValueError('AAC frame too large for ADTS')
    profile = obj - 1
    return bytes((0xFF, 0xF1, (profile << 6) | (freq << 2) | (channels >> 2),
                  ((channels & 3) << 6) | (total >> 11), (total >> 3) & 0xFF,
                  ((total & 7) << 5) | 0x1F, 0xFC))


def extract(source, target):
    data = Path(source).read_bytes()
    if len(data) < 48 or data[4:8] != b'lark' or data[36:40] != b'mdat':
        raise ValueError('Not the expected lark/mdat .vemedia layout')
    pos = 48
    config = None
    rate = None
    count = 0
    first = last = None
    with open(target, 'wb') as out:
        while pos + 8 <= len(data):
            size = u32(data, pos)
            tag = data[pos + 4:pos + 8]
            if size < 8:
                raise ValueError(f'Invalid record size at {pos}')
            if pos + size > len(data):
                break  # not fully written yet
            if tag == b'SIFO':
                # CAA, stream number, 44.1kHz, ASC length and AudioSpecificConfig.
                caa = data.find(b'CAA\x00\x00\x00\x01', pos + 8, pos + size)
                if caa >= 0:
                    rate = u32(data, caa + 7)
                    asc_len = u32(data, caa + 11)
                    if asc_len < 2 or caa + 15 + asc_len > pos + size:
                        raise ValueError('Invalid AAC config in SIFO')
                    config = data[caa + 15:caa + 15 + asc_len]
            elif tag == b'ES  ':
                if size < 23:
                    raise ValueError(f'Invalid ES record at {pos}')
                if data[pos + 8:pos + 11] == b'\x00\x01\x01':
                    if config is None:
                        raise ValueError('Audio ES encountered before CAA config')
                    bits = int.from_bytes(config[:2], 'big')
                    obj = (bits >> 11) & 31
                    freq = (bits >> 7) & 15
                    channels = (bits >> 3) & 15
                    payload = data[pos + 23:pos + size]
                    stamp = int.from_bytes(data[pos + 11:pos + 19], 'big', signed=True)
                    if first is None:
                        first = stamp
                    last = stamp
                    out.write(adts_header(len(payload), obj, freq, channels))
                    out.write(payload)
                    count += 1
            elif tag != b'SSTR':
                raise ValueError(f'Unknown record at {pos}: {tag!r}')
            pos += size
    if not count:
        raise ValueError('No complete audio frames found')
    print(f'frames={count} sample_rate={rate} ASC={config.hex()} first_pts={first} last_pts={last} '
          f'last_pts_seconds={last / rate:.3f} bytes_consumed={pos}/{len(data)} output={target}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('vemedia')
    parser.add_argument('aac_adts')
    args = parser.parse_args()
    extract(args.vemedia, args.aac_adts)
