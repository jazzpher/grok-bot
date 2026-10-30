"""Test-runner wrapper: become a child subreaper and reap every orphaned
descendant so killed/namespaced grandchildren (chromium inside bwrap) do not
accumulate as zombies under a non-reaping PID 1. Test-side only; does not
touch repo code, limits, or environment."""
import ctypes, os, signal, sys

libc = ctypes.CDLL(None)
libc.prctl(36, 1, 0, 0, 0)  # PR_SET_CHILD_SUBREAPER

child = os.fork()
if child == 0:
    os.execvp(sys.argv[1], sys.argv[1:])

def forward(sig, frame):
    try: os.kill(child, sig)
    except ProcessLookupError: pass
signal.signal(signal.SIGTERM, forward)
signal.signal(signal.SIGINT, forward)

main_status = None
while True:
    try:
        wpid, st = os.waitpid(-1, 0)
    except ChildProcessError:
        break
    except InterruptedError:
        continue
    if wpid == child:
        main_status = st
        if os.WIFSIGNALED(st) and os.WTERMSIG(st) == signal.SIGKILL:
            break  # child gone for good; stragglers get one more sweep below
        continue
    # reaped an orphan
# final sweep for orphans still exiting
while True:
    try:
        wpid, st = os.waitpid(-1, os.WNOHANG)
        if wpid == 0:
            break
    except (ChildProcessError, InterruptedError):
        break
sys.exit(os.waitstatus_to_exitcode(main_status) if main_status is not None else 1)
