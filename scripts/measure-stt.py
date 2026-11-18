#!/usr/bin/env python3
"""Measure stt-server CPU and decode time during a meeting or WAV replay.

Only numeric STT stats are read. Recognized text is never printed or saved.
Replay connects directly to stt-server and does not create an advisor session.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

BYTES_PER_SECOND = 16000 * 2
CHUNK_SAMPLES = 1600  # 100 ms at 16 kHz
HTTP = build_opener(ProxyHandler({}))


def get_json(url):
    with HTTP.open(url, timeout=2) as response:
        return json.load(response)


def find_pid():
    proc = Path('/proc')
    if proc.is_dir():
        matches = []
        for path in proc.iterdir():
            if not path.name.isdigit():
                continue
            try:
                command = (path / 'cmdline').read_bytes().replace(b'\0', b' ')
            except (OSError, PermissionError):
                continue
            if b'stt-server' in command and b'serve' in command:
                matches.append(int(path.name))
        if len(matches) == 1:
            return matches[0]
        return None
    result = subprocess.run(['ps', '-axo', 'pid=,command='], capture_output=True,
                            text=True, check=True)
    matches = []
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) == 2 and 'stt-server' in fields[1] and 'serve' in fields[1]:
            matches.append(int(fields[0]))
    return matches[0] if len(matches) == 1 else None


def cpu_seconds(pid):
    if pid is None:
        return None
    stat = Path(f'/proc/{pid}/stat')
    if stat.exists():
        fields = stat.read_text().rsplit(')', 1)[1].split()
        return (int(fields[11]) + int(fields[12])) / os.sysconf('SC_CLK_TCK')
    result = subprocess.run(['ps', '-p', str(pid), '-o', 'time='],
                            capture_output=True, text=True, check=True)
    value = result.stdout.strip()
    days, _, clock = value.rpartition('-')
    parts = [float(part) for part in clock.split(':')]
    seconds = sum(part * 60 ** index for index, part in enumerate(reversed(parts)))
    return seconds + (int(days) * 86400 if days else 0)


class Monitor:
    def __init__(self, stats_url, pid):
        self.stats_url = stats_url
        self.pid = pid
        self.samples = []
        self.errors = []
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=4)

    def _run(self):
        while not self.stop_event.is_set():
            try:
                before = time.monotonic()
                cpu = cpu_seconds(self.pid)
                stats = get_json(self.stats_url)
                self.samples.append((before, cpu, stats))
            except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
                self.errors.append(str(exc))
            self.stop_event.wait(1)

    def report(self):
        if not self.samples:
            raise RuntimeError('无法读取 STT /stats；请确认服务和 health-port 正在运行')
        start, end = self.samples[0], self.samples[-1]
        elapsed = end[0] - start[0]
        peak = max(len(item[2]) for item in self.samples)
        print(f'采样 {elapsed:.1f}s；最多 {peak} 路 STT 会话')
        if start[1] is not None and end[1] is not None and elapsed > 0:
            cpu = end[1] - start[1]
            print(f'stt-server PID {self.pid}：CPU 累计 +{cpu:.1f}s，'
                  f'平均 {100 * cpu / elapsed:.0f}%（100% = 1 个逻辑核）')
        else:
            print('CPU：未自动找到唯一的 stt-server 进程；可指定 --pid')

        by_source = {}
        for _, _, stats in self.samples:
            for item in stats:
                source = item.get('source_id', '')
                if source:
                    by_source.setdefault(source, []).append(item)
        if not by_source:
            print('没有采到活跃会话。请在会议进行中运行，或使用 replay 模式。')
        for source, points in sorted(by_source.items()):
            first, last = points[0], points[-1]
            audio = float(last['audio_s']) - float(first['audio_s'])
            decode = float(last['decode_ms']) - float(first['decode_ms'])
            if audio <= 0 or decode < 0:
                print(f'{source}：采样期间没有连续音频，或会话发生重连')
                continue
            print(f'{source}：音频 +{audio:.1f}s，解码 +{decode / 1000:.2f}s，'
                  f'解码 RTF {decode / (audio * 1000):.3f}，'
                  f'末次 RMS {last.get("rms_last", "?")}')
        if self.errors:
            print(f'采样错误 {len(self.errors)} 次；最后一次：{self.errors[-1]}',
                  file=sys.stderr)


async def replay(args, monitor):
    try:
        import websockets
    except ImportError as exc:
        raise RuntimeError('回放需要 websockets；请使用 stt-server 的虚拟环境运行') from exc

    health_url = args.stats_url.rsplit('/', 1)[0] + '/healthz'
    health = get_json(health_url)
    if health.get('sessions') != 0:
        raise RuntimeError('已有活跃 STT 会话，停止回放以免干扰会议')
    if health.get('auth') != 'none':
        raise RuntimeError('回放仅支持内部 auth=none 的 STT 服务')

    wav = None
    if args.wav:
        wav = wave.open(str(args.wav), 'rb')  # noqa: SIM115 - closed on every exit path
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(),
                wav.getcomptype()) != (1, 2, 16000, 'NONE'):
            wav.close()
            raise RuntimeError('WAV 必须是未压缩的 16 kHz、单声道、16-bit PCM')
        seconds = min(args.seconds, wav.getnframes() / 16000)
    else:
        seconds = args.seconds
    if seconds < 3:
        if wav:
            wav.close()
        raise RuntimeError('可回放音频不足 3 秒')

    print(f'回放 {seconds:.1f}s {"WAV" if wav else "合成静音"}；'
          '不经过 advisor，不写入会议字幕')
    try:
        async with websockets.connect(args.ws_url, proxy=None, max_size=1 << 20) as ws:
            await ws.send(json.dumps({'type': 'start', 'protocol': 2,
                                      'source_id': f'benchmark:{os.getpid()}',
                                      'rate': 16000, 'channels': 1}))
            greeting = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
            if greeting.get('type') != 'started':
                raise RuntimeError(f'STT 拒绝回放：{greeting.get("code", greeting.get("type"))}')

            async def drain():
                async for raw in ws:
                    message = json.loads(raw)
                    if message.get('type') == 'stopped':
                        return
                    if message.get('type') == 'error':
                        raise RuntimeError(f'STT 回放错误：{message.get("code")}')

            receiver = asyncio.create_task(drain())
            monitor.start()
            started = time.monotonic()
            sent = 0
            try:
                limit = int(seconds * 16000)
                while sent < limit:
                    count = min(CHUNK_SAMPLES, limit - sent)
                    pcm = wav.readframes(count) if wav else b'\0' * (count * 2)
                    if not pcm:
                        break
                    await ws.send(pcm)
                    sent += len(pcm) // 2
                    await asyncio.sleep(max(0, started + sent / 16000 - time.monotonic()))
                    if receiver.done():
                        await receiver
                        raise RuntimeError('STT 在回放结束前关闭了连接')
                await ws.send(json.dumps({'type': 'stop'}))
                await asyncio.wait_for(receiver, timeout=30)
            finally:
                monitor.stop()
                receiver.cancel()
    finally:
        if wav:
            wav.close()


def main():
    parser = argparse.ArgumentParser(description='测量 stt-server CPU 与解码 RTF')
    parser.add_argument('--stats-url', default='http://127.0.0.1:8393/stats')
    parser.add_argument('--pid', type=int, help='stt-server PID；默认自动查找')
    sub = parser.add_subparsers(dest='mode', required=True)
    live = sub.add_parser('live', help='会议进行中被动采样')
    live.add_argument('--seconds', type=float, default=30)
    replay_mode = sub.add_parser('replay', help='会后向 STT 直接回放')
    replay_mode.add_argument('--ws-url', default='ws://127.0.0.1:8392')
    replay_mode.add_argument('--wav', type=Path, help='录音 WAV；不填则回放合成静音')
    replay_mode.add_argument('--seconds', type=float, default=60,
                             help='最多回放时长，默认 60 秒')
    args = parser.parse_args()
    if args.seconds < 3:
        parser.error('--seconds 至少为 3')
    monitor = Monitor(args.stats_url, args.pid or find_pid())
    try:
        if args.mode == 'live':
            monitor.start()
            time.sleep(args.seconds)
            monitor.stop()
        else:
            asyncio.run(replay(args, monitor))
        monitor.report()
    except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
        print(f'测量失败：{exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
