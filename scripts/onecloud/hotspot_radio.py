"""Hotspot radio choices and an independent, reboot-safe confirmation window.

Only radio settings live here; credentials, uplink settings and certificates are untouched.
No radio, regulatory-domain or transmit-power commands run when probing capabilities.
"""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time

CONFIRM_SECONDS = 180
_LOCK = threading.RLock()


class Store:
    def __init__(self, state, base, boot, now, arm, alive):
        self.path = Path(state) / 'hotspot-radio.json'
        self.base, self.boot, self.now = base, boot, now
        self.arm, self.alive = arm, alive

    @contextmanager
    def locked(self):
        with _LOCK, self.path.with_suffix('.lock').open('a+b') as lock:
            if os.name == 'posix':
                os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def read(self):
        try:
            return json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return {'selected': dict(self.base), 'pending': None}

    def write(self, value):
        fd, name = tempfile.mkstemp(prefix=self.path.name + '.', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as out:
                json.dump(value, out)
                out.flush()
                os.fsync(out.fileno())
            os.replace(name, self.path)
            if os.name == 'posix':
                directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            Path(name).unlink(missing_ok=True)

    def snapshot(self):
        with self.locked():
            return self.read()

    def effective(self):
        data = self.snapshot()
        return (data['pending']['candidate'] if data.get('pending') else data['selected']).copy()

    def expired(self, pending):
        return pending['boot'] != self.boot or self.now() >= pending['deadline']

    def recover(self):
        """Called before supervisor configures any radio, including after a power cut."""
        with self.locked():
            data = self.read()
            pending = data.get('pending')
            if pending and (self.expired(pending) or not self.alive(pending['id'])):
                self.revert(data)
                return True
            if data.pop('restart', None):
                self.write(data)  # this supervisor start is already applying the restored settings
            return False

    def revert(self, data, token=None):
        data.update(pending=None, result='reverted')
        if token:
            data['restart'] = {'id': token, 'after': self.now() + 2}
        self.write(data)

    def begin(self, candidate):
        with self.locked():
            data = self.read()
            if data.get('pending') or data.get('restart'):
                raise ValueError('请先确认或恢复上一次无线设置。')
            if candidate == data['selected']:
                return None
            token = secrets.token_hex(12)
            try:
                self.arm(token)
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                raise ValueError('自动回退未就绪，设置未应用。') from error
            data.update(pending=dict(id=token, candidate=candidate, boot=self.boot,
                                     deadline=self.now() + CONFIRM_SECONDS), result='pending')
            self.write(data)
            return token

    def pending_for(self, data, token):
        pending = data.get('pending')
        if not pending or not secrets.compare_digest(pending['id'], token):
            raise ValueError('本次设置已结束，请刷新页面。')
        return pending

    def confirm(self, token, measured):
        with self.locked():
            data = self.read()
            pending = self.pending_for(data, token)
            if self.expired(pending):
                raise ValueError('确认已超时，正在恢复原设置。')
            if not measured or any(measured.get(k) != v for k, v in pending['candidate'].items()):
                raise ValueError('实际无线状态尚未匹配，请等待或恢复原设置。')
            data.update(selected=pending['candidate'], pending=None, result='confirmed')
            self.write(data)

    def cancel(self, token):
        with self.locked():
            data = self.read()
            self.pending_for(data, token)
            self.revert(data, token)

    def expire(self, token):
        with self.locked():
            data = self.read()
            pending = data.get('pending')
            if pending and pending['id'] == token and self.expired(pending):
                self.revert(data, token)
                return True
            return False

    def acknowledge_restart(self, token):
        with self.locked():
            data = self.read()
            if data.get('restart', {}).get('id') == token:
                data.pop('restart')
                self.write(data)


def capabilities(text):
    """Conservative intersection of iw's permitted channels and our hostapd generator.

    DFS/no-IR channels are omitted. Every secondary channel must also be permitted.
    The existing hostapd generator implements 2.4 GHz at 20 MHz and two 5 GHz blocks.
    """
    result = {}
    for section in re.split(r'\bBand \d+:', text)[1:]:
        channels = {}
        for match in re.finditer(r'\*\s+(\d+)(?:\.0)? MHz \[(\d+)\]([^\n]*)', section):
            freq, ch, flags = int(match[1]), int(match[2]), match[3].lower()
            if any(flag in flags for flag in ('disabled', 'no ir', 'no-ir', 'radar')):
                continue
            if 2400 < freq < 2500 and 1 <= ch <= 11 or ch in (36, 40, 44, 48, 149, 153, 157, 161, 165) and 5000 < freq < 5900:
                channels[ch] = [20]
        for block in ((36, 40, 44, 48), (149, 153, 157, 161)):
            if 'HT20/HT40' in section:
                for pair in (block[:2], block[2:]):
                    if all(ch in channels for ch in pair):
                        for ch in pair:
                            channels[ch].append(40)
                if 'VHT Capabilities' in section and all(ch in channels for ch in block):
                    for ch in block:
                        channels[ch].append(80)
        for ch, widths in channels.items():
            result.setdefault('2.4' if ch <= 14 else '5', {})[ch] = widths
    return result


def validate(value, caps):
    try:
        band = value['band']
        channel, width = int(value['channel']), int(value['width'])
        if width not in caps.get(band, {}).get(channel, []):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ValueError('频段、信道或频宽不可用，请刷新后重新选择。') from None
    return dict(band=band, channel=channel, width=width)


def actual(text):
    if not re.search(r'^\s*type AP\s*$', text, re.M):
        return None
    found = re.search(r'channel (\d+) \((\d+) MHz\), width: (\d+) MHz', text)
    if not found:
        return None
    channel, frequency, width = map(int, found.groups())
    return dict(band='2.4' if frequency < 2500 else '5', channel=channel,
                width=width, frequency=frequency)


def command(argv):
    return subprocess.run(argv, capture_output=True, text=True, timeout=15, check=True).stdout


def unit(token):
    if not re.fullmatch(r'[0-9a-f]{24}', token):
        raise ValueError('Invalid radio transaction')
    return 'dashplay-radio-' + token


def system_store(root):
    root = Path(root).resolve()
    config = json.loads((root / 'service.json').read_text(encoding='utf-8'))
    channel = int(config['channel'])
    base = dict(band='2.4' if channel <= 14 else '5', channel=channel,
                width=20 if channel <= 14 or channel == 165 else int(config.get('width', 80)))
    def arm(token):
        command(['systemd-run', '--quiet', '--collect', '--unit=' + unit(token),
                 '--property=Type=exec', '--property=Restart=on-failure', '--property=RestartSec=2',
                 sys.executable, str(root / 'hotspot_radio.py'), '--guard', str(root), token])
    def alive(token):
        try:
            return command(['systemctl', 'is-active', unit(token)]).strip() == 'active'
        except (OSError, subprocess.SubprocessError):
            return False
    return Store(root / 'state', base, Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                 lambda: time.clock_gettime(time.CLOCK_BOOTTIME), arm, alive)


class Radio:
    def __init__(self, store, interface):
        self.store, self.interface = store, interface

    def inspect(self):
        data = self.store.snapshot()
        data.update(capabilities={}, actual=None, remaining=0)
        try:
            info = command(['iw', 'dev', self.interface(), 'info'])
            data['actual'] = actual(info)
            phy = re.search(r'\bwiphy (\d+)\b', info)
            if phy:
                data['capabilities'] = capabilities(command(['iw', 'phy', 'phy' + phy[1], 'info']))
        except (OSError, subprocess.SubprocessError):
            pass
        if data.get('pending'):
            data['remaining'] = max(0, int(data['pending']['deadline'] - self.store.now()))
        return data

    def begin(self, form):
        return self.store.begin(validate(form, self.inspect()['capabilities']))

    def confirm(self, token):
        self.store.confirm(token, self.inspect()['actual'])


def guard(store, token, restart, sleep=time.sleep):
    """Runs in its own systemd unit; it never belongs to dashplay.service's cgroup."""
    seen, initial_wait = False, 0
    while True:
        store.expire(token)
        data = store.snapshot()
        recovery = data.get('restart', {})
        if recovery.get('id') == token:
            seen = True
            if store.now() >= recovery['after']:
                try:
                    restart()
                except (OSError, subprocess.SubprocessError):
                    sleep(2)
                    continue  # retain the durable restart request until systemd accepts it
                store.acknowledge_restart(token)
                return
        elif (data.get('pending') or {}).get('id') == token:
            seen = True
        elif seen or initial_wait >= 20:
            return
        initial_wait += 1
        sleep(1)


if __name__ == '__main__':
    if len(sys.argv) != 4 or sys.argv[1] != '--guard':
        raise SystemExit('Usage: hotspot_radio.py --guard ROOT TRANSACTION')
    unit(sys.argv[3])
    guard(system_store(sys.argv[2]), sys.argv[3],
          lambda: command(['systemctl', 'restart', '--no-block', 'dashplay.service']))
