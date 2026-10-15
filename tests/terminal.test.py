import errno
import fcntl
import os
import pty
import select
import signal
import subprocess
import sys
import time


def interrupted_output(command, signo, color, terminal):
    master, slave = pty.openpty() if terminal else os.pipe()
    process = subprocess.Popen(
        command + ["-Cn" if color else "-Mn", '"x" * 1000000'],
        stdin=subprocess.DEVNULL,
        stdout=slave,
        stderr=subprocess.PIPE,
    )
    os.close(slave)
    output = bytearray()
    deadline = time.monotonic() + 30
    try:
        # Read part of a single large JSON string, before its closing color reset.
        assert select.select([master], [], [], 30)[0], "no output before signal"
        output.extend(os.read(master, 4096))
        assert b"x" in output, "did not reach string output"
        assert b"\x1b[0m" not in output, "string already finished"
        # Stop the producer and drain its queued output so the reset control
        # has room. Backpressure is checked separately without draining.
        process.send_signal(signal.SIGSTOP)
        _, status = os.waitpid(process.pid, os.WUNTRACED)
        assert os.WIFSTOPPED(status), "could not stop output before signaling"
        while select.select([master], [], [], 0)[0]:
            output.extend(os.read(master, 65536))
        process.send_signal(signo)
        process.send_signal(signal.SIGCONT)
        while True:
            remaining = deadline - time.monotonic()
            assert remaining > 0, "signal did not terminate jq"
            assert select.select([master], [], [], remaining)[0], "output timed out"
            try:
                data = os.read(master, 65536)
            except OSError as error:
                if terminal and error.errno == errno.EIO:
                    break
                raise
            if not data:
                break
            output.extend(data)
        assert process.wait(timeout=5) == -signo, "signal exit status changed"
        assert process.stderr.read() == b"", "unexpected diagnostic"
        if terminal and color:
            assert output.endswith(b"\x1b[0m"), "terminal color was not reset"
        elif not color:
            assert b"\x1b" not in output, "monochrome output acquired color escapes"
        else:
            assert not output.endswith(b"\x1b[0m"), "reset added to non-terminal output"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stderr.close()
        os.close(master)


def blocked_output(command, signo, ignore_interrupt=False):
    master, slave = pty.openpty()
    original_flags = fcntl.fcntl(slave, fcntl.F_GETFL)
    process = subprocess.Popen(
        command + ["-Cn", '"x" * 1000000'],
        stdin=subprocess.DEVNULL,
        stdout=slave,
        stderr=subprocess.PIPE,
        preexec_fn=(lambda: signal.signal(signal.SIGINT, signal.SIG_IGN))
        if ignore_interrupt else None,
    )
    try:
        assert select.select([master], [], [], 30)[0], "no terminal output"
        # Leave the PTY unread, allowing the finite output to fill its buffer.
        time.sleep(0.1)
        assert process.poll() is None, "output did not block"
        assert fcntl.fcntl(slave, fcntl.F_GETFL) == original_flags
        if ignore_interrupt:
            process.send_signal(signal.SIGINT)
            time.sleep(0.1)
            assert process.poll() is None, "inherited SIGINT ignore was lost"
        process.send_signal(signo)
        assert process.wait(timeout=2) == -signo, "blocked output changed signal exit"
        assert process.stderr.read() == b"", "unexpected diagnostic"
        assert fcntl.fcntl(slave, fcntl.F_GETFL) == original_flags
        print("blocked PTY:", signo, "exit:", process.returncode,
              "stdout flags unchanged; ignored SIGINT:", ignore_interrupt)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stderr.close()
        os.close(slave)
        os.close(master)


for signo in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
    interrupted_output(sys.argv[1:], signo, color=True, terminal=True)
    interrupted_output(sys.argv[1:], signo, color=False, terminal=True)
    interrupted_output(sys.argv[1:], signo, color=True, terminal=False)
    blocked_output(sys.argv[1:], signo)
blocked_output(sys.argv[1:], signal.SIGTERM, ignore_interrupt=True)
