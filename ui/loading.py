"""Full-screen loading overlay for cold starts and wake-ups.

A Streamlit run of the Board takes several seconds (schedules, sharp lines,
Kalshi markets, the edge scan) and until now the page simply sat half-drawn
while that happened. This module paints a branded overlay as the very first
element of a run and removes it when the run finishes.

It only shows when the wait is real:
  * the first run of a browser session (cold start / page reload), or
  * the first run after the session sat idle for WAKE_AFTER_SECONDS.
Ordinary reruns (a widget click, the 30-second live-score refresh) never
show it, so it does not flash over a board the user is already reading.

Failure safety: the overlay fades itself out with a CSS animation after
FAILSAFE_SECONDS even if the script never reaches `end()`, so a crash or an
early `st.stop()` cannot leave the app permanently covered.
"""
from __future__ import annotations

import time

import streamlit as st

WAKE_AFTER_SECONDS = 300
FAILSAFE_SECONDS = 45

#: Stages shown under the progress bar, in order.
STEPS = ["Schedules", "Sharp lines", "Kalshi markets", "Scanning edges"]
_MESSAGES = [
    "Pulling today's schedules and scores",
    "Fetching sharp lines",
    "Matching Kalshi markets",
    "Scanning the slate for edges",
]

_SLOT = "_loader_slot"
_ON = "_loader_on"
_LAST = "_loader_last_run"


def _overlay_html(step: int, waking: bool) -> str:
    step = max(0, min(step, len(STEPS) - 1))
    pct = int(round((step + 0.5) / len(STEPS) * 100))
    labels = "".join(
        f'<span class="{"done" if i < step else ("now" if i == step else "")}">{name}</span>'
        for i, name in enumerate(STEPS)
    )
    title = "Waking up" if waking else "Starting up"
    rows = "".join(f'<div class="edge-loader-sk" style="opacity:{o}"></div>' for o in (1, 0.75, 0.5, 0.3))
    return (
        f'<div class="edge-loader" style="--failsafe:{FAILSAFE_SECONDS}s" role="status" aria-live="polite">'
        '<div class="edge-loader-glow"></div>'
        '<div class="edge-loader-in">'
        '<div class="edge-loader-mark">E</div>'
        '<div class="edge-loader-title">Edge</div>'
        f'<div class="edge-loader-sub">{title} · {_MESSAGES[step]}</div>'
        f'<div class="edge-loader-track"><i style="width:{pct}%"></i></div>'
        f'<div class="edge-loader-steps">{labels}</div>'
        f'{rows}'
        '</div></div>'
    )


def begin() -> None:
    """Call first in a run. Paints the overlay when this is a cold or woken run."""
    now = time.time()
    last = st.session_state.get(_LAST)
    first_run = last is None
    waking = (not first_run) and (now - float(last)) > WAKE_AFTER_SECONDS
    slot = st.empty()
    st.session_state[_SLOT] = slot
    st.session_state[_ON] = bool(first_run or waking)
    st.session_state["_loader_waking"] = bool(waking)
    if st.session_state[_ON]:
        slot.markdown(_overlay_html(0, waking), unsafe_allow_html=True)


def step(index: int) -> None:
    """Advance the overlay to stage `index` (no-op when it is not showing)."""
    if not st.session_state.get(_ON):
        return
    slot = st.session_state.get(_SLOT)
    if slot is None:
        return
    try:
        slot.markdown(
            _overlay_html(index, bool(st.session_state.get("_loader_waking"))),
            unsafe_allow_html=True,
        )
    except Exception:
        pass


def end() -> None:
    """Call last in a run (in a `finally`). Removes the overlay."""
    slot = st.session_state.get(_SLOT)
    if slot is not None:
        try:
            slot.empty()
        except Exception:
            pass
    st.session_state[_ON] = False
    st.session_state[_LAST] = time.time()
