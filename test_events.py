"""Offline check for the events calendar monitor: python test_events.py"""
import checker

sent = []
checker.send_message = sent.append


def row(key, title, start, end):
    return (f'<div class="row"><div class="vertical-rl">ACTIVE</div><div class="title-content-layout">'
            f'<div class="white-text exo-bold-400">{title}</div><div>'
            f'<div class="teal-small-bold-text-text">Tag</div><div class="teal-small-text">Desc</div>'
            f'<div class="yellow-text exo-bold-400">START:</div><div class="yellow-text exo-400">{start} UTC</div>'
            f'<div class="yellow-text exo-bold-400">END:</div><div class="yellow-text exo-400">{end} UTC</div>'
            f'<img class="calendarIcon" alt="calendar_icon_{key}"></div></div></div>')


def run(*rows):
    class Resp:
        status_code = 200
        content = "".join(rows).encode()
    checker.fetch = lambda url: Resp
    sent.clear()
    checker.check_events(state)
    return list(sent)


state = {}
past, future = "2020-01-01T00:00:00", "2099-01-01T00:00:00"
sunday = row("event_second_sunday_23", "Second Sunday", past, past)
a = row("event_A_100", "A", past, future)
b = row("event_B_101", "B", past, future)

assert run(sunday, a, b) == []                      # silent baseline
msgs = run(sunday, a, b, row("event_D_104", "D", past, future), row("event_C_102", "C", past, future))
assert len(msgs) == 2 and "Skipped" not in msgs[0] and "Skipped event ID 103 " in msgs[1], msgs
sunday = row("event_second_sunday_23", "Second Sunday", future, future)
assert run(sunday, a, b, row("event_C_102", "C", past, future), row("event_D_104", "D", past, future)) == []  # rollover
msgs = run(sunday, row("event_A_100", "A", past, "2099-02-01T00:00:00"))  # A extended, B/C/D pulled early
assert "End:</b> 2099-01-01T00:00:00 UTC → 2099-02-01" in msgs[0] and sum("removed" in m for m in msgs) == 3, msgs
state["events"]["items"]["event_A_100"]["End"] = past + " UTC"
assert run(sunday) == []  # A ended normally: no removal notice
print("ok")
