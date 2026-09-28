import subprocess
from types import SimpleNamespace

from review_checks import android_lab


class Launcher:
    def __init__(self, exited=False):
        self.exited = exited
        self.terminated = False

    def poll(self):
        return 0 if self.exited else None

    def wait(self, timeout):
        if not self.exited:
            raise subprocess.TimeoutExpired('owned-launcher', timeout)

    def terminate(self):
        self.terminated = self.exited = True


def test_unverified_or_exited_launcher_never_stops_serial(monkeypatch):
    calls = []
    monkeypatch.setattr(android_lab.subprocess, 'run', lambda *a, **k: calls.append(a))
    for verified, exited in [(False, False), (True, True)]:
        launcher = Launcher(exited)
        android_lab.stop_owned_lab(launcher, 'adb', 'emulator-5580', verified)
        assert calls == []
        assert launcher.exited


def test_changed_avd_identity_only_terminates_owned_launcher(monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout='OTHER_AVD\nOK\n')
    monkeypatch.setattr(android_lab.subprocess, 'run', run)
    launcher = Launcher()
    android_lab.stop_owned_lab(launcher, 'adb', 'emulator-5580', True)
    assert launcher.terminated
    assert all(args[-1] != 'kill' for args in calls)


def test_identity_query_timeout_still_reaps_owned_launcher(monkeypatch):
    def run(*args, **kwargs):
        raise subprocess.TimeoutExpired('adb', 5)
    monkeypatch.setattr(android_lab.subprocess, 'run', run)
    launcher = Launcher()
    result = android_lab.stop_owned_lab(launcher, 'adb', 'emulator-5580', True)
    assert launcher.terminated
    assert result['owned_launcher_stopped']
