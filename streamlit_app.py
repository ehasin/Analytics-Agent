"""
streamlit_app.py — Analytics Agent Streamlit MVP

Architecture
────────────
All agent logic lives in code_modules/ (verbatim copy of the Colab PoC).
This file is pure UI + orchestration: session state, threading, logging calls.

Call graph per turn (mirrors the Colab notebook):
  parse_mode_command → interpret_turn → analyst_agent (classify+plan+execute+narrate)
  → update_summary → log to DB (async background thread)
"""

import sys
import os
import time
import uuid
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

# ── Add code_modules to path BEFORE any agent imports ───────────────────────
_ROOT = Path(__file__).parent
sys.path.insert(0, str(_ROOT / "code_modules"))

import streamlit as st

# ── Agent imports ────────────────────────────────────────────────────────────
from _agent.analyst_agent import analyst_agent, MODE_NAMES, extract_user_context
from _agent.conversation_processor import (
    parse_mode_command, CMD_TO_MODE, MODE_TRANSITION_MSGS,
    interpret_turn, update_summary, enrich_trace, trace_total_seconds,
)
from _skills.llm_backends import call_llm, init_backends, MODEL_MAP
from _skills.duckdb_utils import execute_queries
from _data.olist_schema_and_datasets import (
    load_public_ecommerce_datasets, load_schema_context,
)
from streamlit_session_logger import (
    StreamlitSessionLogger, resolve_geo, parse_user_agent,
)

# ═══════════════════════════════════════════════════════════════════════════
# PAGE CONFIG — must be the first Streamlit call
# ═══════════════════════════════════════════════════════════════════════════
st.set_page_config(
    page_title="Analytics Agent",
    page_icon="◆",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ═══════════════════════════════════════════════════════════════════════════
# SESSION STATE DEFAULTS
# ═══════════════════════════════════════════════════════════════════════════
_DEFAULTS = {
    # conversation
    "messages": [],        # [{role, content, transition, sql, mode}]
    "history": [],         # [{question, resolved, narrative, mode, classification}]
    "summary": "",         # rolling session summary
    # mode system
    "current_mode": 0,     # 0=Retrieve, 1=Explore, 2=Reason
    "mode_radio": "auto",  # "auto"|"retrieve"|"explore"|"reason"
    "pending_mode": None,
    "mode_control": "auto",
    # UI state
    "theme": "light",
    "first_prompt_sent": False,
    "confirm_clear": False,
    # backend / auth — owner key loaded from secrets automatically
    # user_clients/user_backend hold an optional per-session override key
    "backend": "groq",
    "user_clients": None,
    "user_backend": None,
    "user_key_active": False,
    "user_ping_msg": "",
    "user_key_remove_confirm": False,
    # run management
    "is_running": False,
    "run_id": None,
    "queued_prompt": None,
    "pre_run_mode": 0,
    # logging
    "session_id": None,
    "chat_id": None,
    "session_initialized": False,
    "turn_num": 0,
}
for _k, _v in _DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v

# Thread communication — stored in session_state to survive hot-reloads
if "_results" not in st.session_state:
    st.session_state._results = {}
if "_stop_flags" not in st.session_state:
    st.session_state._stop_flags = {}
_results    = st.session_state._results
_stop_flags = st.session_state._stop_flags

# Sync bot-requested mode change before any widget renders
if st.session_state.pending_mode is not None:
    _pm = st.session_state.pending_mode
    st.session_state.pending_mode = None
    if st.session_state.mode_control == "manual" and 0 <= _pm <= 2:
        st.session_state.mode_radio = ["retrieve", "explore", "reason"][_pm]

_running = st.session_state.is_running
_ci      = st.session_state.current_mode   # 0 / 1 / 2

# ═══════════════════════════════════════════════════════════════════════════
# THEME TOKENS
# ═══════════════════════════════════════════════════════════════════════════
_is_dark = st.session_state.theme == "dark"
if _is_dark:
    bg        = "#0e1117"; bg2       = "#161b22"; bg_card   = "#1c2129"
    tp        = "#c9d1d9"; ts        = "#7d8590"
    accent    = "#58a6ff"; border    = "#30363d"; code_bg   = "#161b22"
    track_col = "#30363d"; knob_col  = "#58a6ff"
    sb_border = "#484f58"  # brighter border for sidebar controls in dark mode
else:
    bg        = "#ffffff"; bg2       = "#f6f8fa"; bg_card   = "#f6f8fa"
    tp        = "#1f2328"; ts        = "#656d76"
    accent    = "#0969da"; border    = "#d0d7de"; code_bg   = "#f6f8fa"
    track_col = "#d0d7de"; knob_col  = "#0969da"
    sb_border = "#d0d7de"

_MODE_NAMES = ["Retrieve", "Explore", "Reason"]
_MODE_DESC  = [
    "Quick factual answers — counts, totals, direct lookups.",
    "Broader analysis — trends, patterns, comparisons across dimensions.",
    "Deep reasoning — causal explanation, interpretation, hypothesis validation.",
]
_TRANSITIONS = {
    (0, 1): "Switching to Explore — broadening the analysis scope.",
    (0, 2): "Switching to Reason — engaging advanced reasoning for deeper analysis.",
    (1, 0): "Switching to Retrieve — narrowing to a quick factual answer.",
    (1, 2): "Switching to Reason — engaging advanced reasoning for deeper analysis.",
    (2, 0): "Switching to Retrieve — quick factual answer.",
    (2, 1): "Switching to Explore — broadening scope.",
}
_SUGGESTIONS = [
    "Overview of available data",
    "How many customers do we have?",
    "Top 5 product categories by revenue",
    "Monthly sales trend for 2017–2018",
    "Customer satisfaction trend analysis",
]
_BACKEND_LABELS   = {"groq": "Groq", "claude": "Claude"}
_BACKEND_MODELS = {
    "groq":   ["llama-3.1-8b", "llama-3.3-70b", "gpt-oss-120b"],
    "claude": ["Haiku 4.5", "Sonnet 4.6", "Opus 4.7"],
}

# ═══════════════════════════════════════════════════════════════════════════
# GLOBAL CSS
# ═══════════════════════════════════════════════════════════════════════════
st.markdown(f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap');

.stApp {{ background:{bg}; font-family:'Outfit',sans-serif; color:{tp}; }}

/* ── Sidebar ── */
section[data-testid="stSidebar"] {{
    background:{bg2}; border-right:1px solid {border}; padding-top:0;
    z-index:1001 !important; overflow:visible !important;
}}
section[data-testid="stSidebar"] > div:first-child {{
    padding-top:1rem; padding-left:1.5rem; padding-right:1.5rem;
    overflow-x:visible !important;
}}
section[data-testid="stSidebar"] .stMarkdown,
section[data-testid="stSidebar"] .stMarkdown p,
section[data-testid="stSidebar"] .stMarkdown span {{
    font-family:'Outfit',sans-serif; color:{tp} !important;
}}
/* Sidebar labels + radio text → muted. Expander headers targeted separately,
   but we MUST NOT override the icon <span> — it uses Material Symbols font. */
section[data-testid="stSidebar"] label,
section[data-testid="stSidebar"] .stRadio label {{
    font-family:'Outfit',sans-serif; color:{ts} !important;
}}
/* Expander header text only (the <p> inside summary, not the icon span) */
section[data-testid="stSidebar"] [data-testid="stExpander"] summary p,
section[data-testid="stSidebar"] details > summary p {{
    font-family:'Outfit',sans-serif !important; color:{ts} !important;
}}
/* Section bold headings (Display theme, Dataset, LLM Backend) */
section[data-testid="stSidebar"] .sidebar-section-head {{
    font-family:'Outfit',sans-serif; font-size:0.85rem; font-weight:600;
    color:{tp}; margin:0 0 6px 0;
}}
/* Force radio item text colour — Streamlit's emotion class `.st-bm` sits on a
   descendant <div> inside the <label> and overrides the label's own color.
   Target every descendant of the radio label to defeat it. */
section[data-testid="stSidebar"] .stRadio label *,
section[data-testid="stSidebar"] [data-testid="stRadio"] label * {{
    color:{ts} !important;
}}
/* Brighter borders for sidebar expanders + buttons in dark mode
   (keeps them visible against the dark sidebar background). */
section[data-testid="stSidebar"] [data-testid="stExpander"] {{
    border-color:{sb_border} !important;
}}
section[data-testid="stSidebar"] [data-testid="stExpander"] details {{
    border-color:{sb_border} !important;
}}
section[data-testid="stSidebar"] .stButton > button {{
    border-color:{sb_border} !important;
}}

/* ── Chat messages ── */
.stChatMessage {{
    font-family:'Outfit',sans-serif; font-size:0.93rem;
    line-height:1.65; color:{tp} !important;
}}
.stChatMessage p,.stChatMessage li,.stChatMessage td,.stChatMessage th {{
    color:{tp} !important;
}}
.stChatMessage code {{
    font-family:'IBM Plex Mono',monospace; font-size:0.82rem;
    background:{code_bg}; color:{tp};
}}
.stChatMessage h1,.stChatMessage h2,.stChatMessage h3,.stChatMessage h4 {{
    font-size:1rem !important; font-weight:600 !important;
    line-height:1.4 !important; margin:0.6rem 0 0.2rem 0 !important;
    color:{tp} !important;
}}

/* ── Sidebar mode slider ── */
section[data-testid="stSidebar"] [data-testid="stSlider"] {{
    padding:0 !important; background:none !important;
    border:none !important; box-shadow:none !important;
}}
section[data-testid="stSidebar"] [data-testid="stSlider"] div {{
    background:transparent !important; background-color:transparent !important;
    border-radius:0 !important;
}}
section[data-testid="stSidebar"] [data-testid="stSliderThumbValue"],
section[data-testid="stSidebar"] [data-testid="stSliderTickBar"] {{
    display:none !important;
}}
section[data-testid="stSidebar"] [data-testid="stSlider"] [role="slider"] {{
    background:{knob_col} !important; background-color:{knob_col} !important;
    border:none !important; width:14px !important; height:14px !important;
    border-radius:50% !important; box-shadow:0 1px 4px rgba(0,0,0,.22) !important;
}}
section[data-testid="stSidebar"] [data-testid="stSlider"] [data-testid="stWidgetLabel"] {{
    display:none !important;
}}

/* ── Auto/Manual segmented control ── */
section[data-testid="stSidebar"] [data-baseweb="button-group"] {{
    display:inline-flex !important; flex-direction:row !important;
    gap:0 !important; border:1px solid {border} !important;
    border-radius:6px !important; overflow:hidden !important;
    background:transparent !important; padding:0 !important;
    width:auto !important; flex-wrap:nowrap !important;
}}
section[data-testid="stSidebar"] [data-baseweb="button-group"] button {{
    font-family:'IBM Plex Mono',monospace !important; font-size:11px !important;
    padding:3px 12px !important; min-height:0 !important; height:26px !important;
    border:none !important; border-radius:0 !important;
    background:transparent !important; color:{ts} !important;
    white-space:nowrap !important; flex:0 0 auto !important;
}}
section[data-testid="stSidebar"] [data-baseweb="button-group"] button[kind="segmented_controlActive"],
section[data-testid="stSidebar"] [data-baseweb="button-group"] button[kind="segmented_controlActive"] * {{
    background:{accent} !important; color:#ffffff !important;
}}
section[data-testid="stSidebar"] [data-testid="stWidgetLabel"]:has(+ [data-baseweb="button-group"]) {{
    display:none !important;
}}

/* ── Slider labels row ── */
.slider-labels {{
    display:flex; justify-content:space-between; width:100%;
    margin-top:-30px; margin-bottom:4px; padding:0;
    font-family:'IBM Plex Mono',monospace; font-size:10px; color:{ts};
}}
.slider-labels .on {{ color:{tp}; font-weight:700; }}

/* ── Mode description ── */
.mode-desc-sidebar {{
    font-family:'IBM Plex Mono',monospace; font-size:11px;
    color:{ts}; margin:0 0 10px 0; line-height:1.4;
}}

/* ── Mode badge in messages ── */
.mode-badge {{
    display:inline-block;
    font-family:'IBM Plex Mono',monospace; font-size:10px;
    color:{ts}; background:{bg2}; border:1px solid {border};
    border-radius:4px; padding:1px 6px; margin-bottom:6px;
    letter-spacing:.04em;
}}

/* ── Transition message ── */
.transition-msg {{
    font-family:'IBM Plex Mono',monospace; font-size:0.75rem;
    color:{ts}; padding:4px 0; font-style:italic;
}}

/* ── Tooltip ── */
.mb-tip-host {{
    position:relative; display:inline-flex;
    align-items:center; cursor:help; margin-left:4px;
    overflow:visible !important;
}}
.mb-tip {{
    display:none; position:absolute; bottom:calc(100% + 6px); right:0;
    left:auto; width:210px; padding:9px 12px; background:{bg_card};
    color:{tp}; border:1px solid {border}; border-radius:7px;
    font-family:'IBM Plex Mono',monospace; font-size:11px; line-height:1.55;
    box-shadow:0 4px 16px rgba(0,0,0,.12); z-index:999999 !important;
    pointer-events:none;
}}
.mb-tip-host:hover .mb-tip {{ display:block !important; }}

/* ── About box ── */
.about-box {{
    background:{bg_card}; border:1px solid {border}; border-radius:8px;
    padding:14px; font-size:0.84rem; line-height:1.6; color:{ts};
}}
.about-box a {{ color:{accent}; text-decoration:none; }}
.about-box a:hover {{ text-decoration:underline; }}

/* ── Primary button text in sidebar ── */
section[data-testid="stSidebar"] [data-testid="stBaseButton-primary"],
section[data-testid="stSidebar"] [data-testid="stBaseButton-primary"] *,
section[data-testid="stSidebar"] button[kind="primary"],
section[data-testid="stSidebar"] button[kind="primary"] * {{
    color:#ffffff !important; fill:#ffffff !important;
}}

/* ── Suggestion cards ── */
[data-testid="stBaseButton-secondary"] {{
    background-color:{bg_card} !important; color:{tp} !important;
    border:1px solid {border} !important; font-family:'Outfit',sans-serif !important;
}}
[data-testid="stBaseButton-secondary"]:hover {{
    background-color:{bg2} !important; border-color:{ts} !important;
}}

/* ── Chat input bar ── */
[data-testid="stBottom"],[data-testid="stBottom"] > div {{ background:{bg} !important; }}
[data-testid="stChatInputContainer"] {{
    background:{bg2} !important; border:1px solid {border} !important;
    border-radius:12px !important;
}}
[data-testid="stChatInputContainer"] textarea {{
    background:transparent !important; color:{tp} !important;
}}
[data-testid="stChatInputContainer"] textarea::placeholder {{ color:{ts} !important; }}

/* ── Ready indicator ── */
.ready-dot {{
    display:inline-block; width:8px; height:8px; border-radius:50%;
    background:#3fb950; margin-right:6px; vertical-align:middle;
}}
.connecting-dot {{
    display:inline-block; width:8px; height:8px; border-radius:50%;
    background:#e3b341; margin-right:6px; vertical-align:middle;
    animation: pulse 1.2s ease-in-out infinite;
}}
@keyframes pulse {{ 0%,100%{{opacity:1}} 50%{{opacity:.3}} }}

/* ── Show SQL expander (main area) ── */
/* Expander border */
.stMain [data-testid="stExpander"] {{
    border:1px solid {sb_border} !important;
    border-radius:6px !important;
    background:{bg2} !important;
}}
/* Header row background */
.stMain [data-testid="stExpander"] summary {{
    background:{bg2} !important;
    border-radius:6px !important;
}}
/* Chevron SVG icon */
.stMain [data-testid="stExpander"] summary svg {{
    fill:{ts} !important; color:{ts} !important;
}}
/* "Show SQL" label text */
.stMain [data-testid="stExpander"] summary p,
.stMain [data-testid="stExpander"] summary span:not([data-testid]) {{
    color:{ts} !important; font-family:'Outfit',sans-serif !important;
}}
/* Code block container inside expander */
.stMain [data-testid="stExpander"] [data-testid="stCodeBlock"] {{
    background:{code_bg} !important;
}}
.stMain [data-testid="stExpander"] pre,
.stMain [data-testid="stExpander"] code {{
    background:{code_bg} !important;
    color:{tp} !important;
    font-family:'IBM Plex Mono',monospace !important;
    font-size:0.8rem !important;
}}
/* Syntax-highlighted tokens — keep them readable in dark mode */
.stMain [data-testid="stExpander"] .token.keyword {{ color:#ff7b72 !important; }}
.stMain [data-testid="stExpander"] .token.string  {{ color:#a5d6ff !important; }}
.stMain [data-testid="stExpander"] .token.number  {{ color:#79c0ff !important; }}
.stMain [data-testid="stExpander"] .token.comment {{ color:{ts} !important; font-style:italic; }}

/* ── Pipeline step display (Colab-style) ── */
.pipeline-steps {{
    font-family:'IBM Plex Mono',monospace; font-size:0.78rem;
    color:{ts}; line-height:1.9;
}}
.pipeline-steps .step-arrow {{ color:{accent}; font-weight:700; margin-right:4px; }}
.pipeline-steps .step-label {{ color:{ts}; }}
.pipeline-steps .step-value {{ color:{tp}; }}
.pipeline-steps .step-sub   {{ margin-left:1.4em; }}

/* ── Hide Streamlit chrome ── */
header[data-testid="stHeader"] {{ background:transparent !important; }}
#MainMenu {{ visibility:hidden; }}
footer {{ visibility:hidden; }}
[data-testid="stToolbar"] {{ visibility:hidden !important; }}
button[kind="headerNoPadding"] {{ display:none !important; }}
[data-testid="stDeployButton"] {{ display:none !important; }}
</style>
""", unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════════
# CACHED RESOURCES  (global — one instance for all sessions)
# ═══════════════════════════════════════════════════════════════════════════

@st.cache_resource(show_spinner=False)
def _load_data():
    """Download all 7 Olist CSVs once; cached across sessions."""
    return load_public_ecommerce_datasets(verbose=False)


@st.cache_resource(show_spinner=False)
def _load_schema():
    """Load data_model.json once; cached across sessions."""
    _, schema_text, _ = load_schema_context(_ROOT / "data_model.json")
    return schema_text


@st.cache_resource(show_spinner=False)
def _start_data_preload():
    """Kick off CSV download in a background thread. Returns immediately.
    Subsequent calls to _load_data() / _load_schema() return from cache instantly
    (or wait the remaining download time if still in progress).
    """
    def _bg():
        try:
            _load_data()
            _load_schema()
        except Exception:
            pass
    threading.Thread(target=_bg, daemon=True).start()


@st.cache_resource(show_spinner=False)
def _get_owner_client():
    """Load the owner's Groq key from secrets/env and init a shared client.
    Returns the clients dict, or None if no key is configured.
    Cached globally — one client instance for all sessions using the default key.
    """
    key = None
    # Try st.secrets (bracket access — .get() can silently fail in cache context)
    try:
        key = st.secrets["GROQ_API_KEY"]
    except Exception:
        pass
    # Fall back to environment variable
    if not key:
        key = os.environ.get("GROQ_API_KEY")
    if not key:
        return None
    try:
        result = init_backends(
            backends=["groq"],
            secret_names={},
            test=False,          # skip ping on startup — first turn will surface auth errors
            api_keys={"groq": key},
        )
        return result["clients"]
    except Exception:
        return None


def _get_active_clients():
    """Return (clients, backend) — user override takes precedence over owner key."""
    if st.session_state.user_key_active and st.session_state.user_clients:
        return st.session_state.user_clients, st.session_state.user_backend
    owner = _get_owner_client()
    if owner:
        return owner, "groq"
    return None, None


@st.cache_resource(show_spinner=False)
def _get_db_logger():
    """Create the usage DB logger once; cached across sessions."""
    neon_url = None
    try:
        neon_url = st.secrets.get("NEON_DB_URL") or os.environ.get("NEON_DB_URL")
    except Exception:
        neon_url = os.environ.get("NEON_DB_URL")
    logger = StreamlitSessionLogger(db_url=neon_url or None)
    logger.ping()
    return logger


# ═══════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ═══════════════════════════════════════════════════════════════════════════
with st.sidebar:
    # ── Wordmark ────────────────────────────────────────────────
    st.markdown(
        f"<div style='padding:20px 0 16px 0;'>"
        f"<h2 style='margin:0 0 12px 0;font-weight:700;letter-spacing:-.5px;"
        f"color:{tp};font-size:1.3rem;'>◆ Analytics Agent</h2></div>",
        unsafe_allow_html=True,
    )

    # ── About ────────────────────────────────────────────────────
    with st.expander("About", expanded=False):
        st.markdown(
            f'<div class="about-box">'
            f"AI-powered natural language analytics, with model governance for reliable results.<br><br>"
            f"<strong>Developer:</strong> Evgeni Hasin<br>"
            f'<a href="https://www.linkedin.com/in/evgenihasin/" target="_blank">LinkedIn</a>'
            f' &nbsp;·&nbsp; '
            f'<a href="https://github.com/ehasin" target="_blank">GitHub</a>'
            f"</div>",
            unsafe_allow_html=True,
        )

    st.divider()

    # ── Display theme ────────────────────────────────────────────
    st.markdown('<div class="sidebar-section-head">Display theme</div>',
                unsafe_allow_html=True)
    theme_choice = st.radio(
        "Theme",
        ["Light", "Dark"],
        index=1 if _is_dark else 0,
        horizontal=True,
        disabled=_running,
        label_visibility="collapsed",
        key="theme_radio",
    )
    new_theme = "dark" if "Dark" in theme_choice else "light"
    if new_theme != st.session_state.theme and not _running:
        st.session_state.theme        = new_theme
        st.session_state.pending_mode = _ci
        st.rerun()

    st.divider()

    # ── Analysis Mode ─────────────────────────────────────────────
    st.markdown(
        f'<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">'
        f'<span style="font-family:\'Outfit\',sans-serif;font-size:0.75rem;'
        f'font-weight:600;letter-spacing:.06em;color:{ts};">ANALYSIS MODE</span>'
        f'<div class="mb-tip-host">'
        f'<svg width="14" height="14" viewBox="0 0 16 16" style="opacity:.5;">'
        f'<circle cx="8" cy="8" r="7.5" fill="none" stroke="{ts}" stroke-width="1.2"/>'
        f'<text x="8" y="12.5" text-anchor="middle" font-size="10" fill="{ts}" '
        f'font-family="sans-serif" font-weight="600">?</text></svg>'
        f'<div class="mb-tip">'
        f'<b>Auto</b> — agent picks the best mode each turn.<br>'
        f'<b>Retrieve</b> — quick factual answers.<br>'
        f'<b>Explore</b> — trends, patterns, comparisons.<br>'
        f'<b>Reason</b> — causal explanation, deep conclusions.<br><br>'
        f'Slash commands work too — type in the chat input.</div>'
        f'</div></div>',
        unsafe_allow_html=True,
    )

    # 4 exclusive radio options (stacked vertically)
    _RADIO_OPTS  = ["Auto", "Retrieve", "Explore", "Reason"]
    _radio_idx   = {"auto": 0, "retrieve": 1, "explore": 2, "reason": 3}.get(
        st.session_state.mode_radio, 0
    )
    # No `key=` here — with a key, Streamlit uses st.session_state[key] (last user click)
    # and IGNORES the `index` parameter, preventing programmatic updates (slash commands,
    # agent mode-switches) from being reflected in the widget. Without a key, `index`
    # always drives the displayed selection.
    _mode_radio  = st.radio(
        "mode_radio",
        options=_RADIO_OPTS,
        index=_radio_idx,
        horizontal=False,
        disabled=_running,
        label_visibility="collapsed",
    )
    if _mode_radio is not None and not _running:
        _new_mr = _mode_radio.lower()
        if _new_mr != st.session_state.mode_radio:
            st.session_state.mode_radio = _new_mr
            if _new_mr == "auto":
                st.session_state.mode_control = "auto"
            else:
                st.session_state.mode_control = "manual"
                st.session_state.current_mode = _RADIO_OPTS.index(_mode_radio) - 1
            st.rerun()

    # Current Mode label + value — always shows selected mode, even while running
    _cur_name = _MODE_NAMES[_ci]
    _locked   = " · Manual" if st.session_state.mode_control == "manual" else ""
    _cur_html = (
        f"<span style='color:{ts};'>Current Mode:</span>&nbsp;&nbsp;"
        f"<span style='color:{tp};font-weight:600;'>{_cur_name}</span>"
        f"<span style='color:{ts};'>{_locked}</span>"
    )
    st.markdown(
        f"<div style='font-family:\"Outfit\",sans-serif;font-size:0.82rem;"
        f"margin:12px 0 4px 0;'>{_cur_html}</div>",
        unsafe_allow_html=True,
    )

    # ── Slash-command hint (bottom of sidebar) ────────────────────
    st.markdown("<div style='height:10px;'></div>", unsafe_allow_html=True)
    st.markdown(
        f"<div style='font-family:\"IBM Plex Mono\",monospace;font-size:0.8em;"
        f"color:{tp};opacity:0.6;line-height:1.9;'>"
        f"Change mode inline:<br>"
        f"<code style='background:none;color:{tp};font-size:0.75em;'>/auto</code>&ensp;"
        f"<code style='background:none;color:{tp};font-size:0.75em;'>/retrieve</code>&ensp;"
        f"<code style='background:none;color:{tp};font-size:0.75em;'>/explore</code>&ensp;"
        f"<code style='background:none;color:{tp};font-size:0.75em;'>/reason</code>"
        f"</div>",
        unsafe_allow_html=True,
    )
    
    st.divider()  

    # ── Dataset ──────────────────────────────────────────────────
    st.markdown(
        f'<div class="sidebar-section-head">Dataset</div>'
        f'<div style="font-family:\'Outfit\',sans-serif;font-size:0.82rem;line-height:1.5;">'
        f'<a href="https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce" '
        f'target="_blank" style="color:{accent};text-decoration:none;">'
        f'E-Commerce Public Dataset by Olist</a></div>'
        f'<div style="font-family:\'IBM Plex Mono\',monospace;font-size:0.68rem;'
        f'color:{ts};margin-top:3px;">~100k orders · 2016–2018</div>',
        unsafe_allow_html=True,
    )

    st.divider()

    # ── LLM Backend ──────────────────────────────────────────────
    _active_be_label = (
        _BACKEND_LABELS.get(st.session_state.user_backend, "Groq")
        if st.session_state.user_key_active else "Groq"
    )
    _key_source = "user key" if st.session_state.user_key_active else "default key"
    _model_be   = st.session_state.user_backend or "groq"
    _model_lis  = "".join(
        f"<li style='margin:0;padding:0;'>{m}</li>"
        for m in _BACKEND_MODELS.get(_model_be, [])
    )
    st.markdown(
        f'<div class="sidebar-section-head">LLM Backend</div>'
        f"<div style='font-family:\"IBM Plex Mono\",monospace;font-size:0.7rem;"
        f"color:{ts};line-height:1.7;margin-bottom:6px;'>"
        f"<span class='ready-dot'></span>{_active_be_label} · {_key_source}"
        f"<ul style='margin:2px 0 0 14px;padding:0;list-style:disc;'>{_model_lis}</ul>"
        f"</div>",
        unsafe_allow_html=True,
    )

    # ── Use your own Key (expander styled as compact button) ─────
    _key_label = "🔑 Using your key ✓" if st.session_state.user_key_active else "Use your own Key"
    with st.expander(_key_label, expanded=False):
        if st.session_state.user_key_active:
            # ── Active state: show status + remove button ─────────
            _abe = _BACKEND_LABELS.get(st.session_state.user_backend, "Groq")
            st.markdown(
                f"<p style='font-family:\"IBM Plex Mono\",monospace;font-size:11px;"
                f"color:{ts};margin:0 0 8px 0;'>Using your {_abe} key.</p>",
                unsafe_allow_html=True,
            )
            if st.session_state.user_ping_msg:
                st.caption(f'Connected · "{st.session_state.user_ping_msg}"')

            if not st.session_state.user_key_remove_confirm:
                if st.button("Remove key → use default", use_container_width=True,
                             disabled=_running, key="ownkey_remove"):
                    st.session_state.user_key_remove_confirm = True
                    st.rerun()
            else:
                st.caption("Remove your key and revert to the default?")
                _rc1, _rc2 = st.columns(2)
                with _rc1:
                    if st.button("Yes, remove", use_container_width=True, type="primary",
                                 disabled=_running, key="ownkey_remove_yes"):
                        st.session_state.user_clients    = None
                        st.session_state.user_backend    = None
                        st.session_state.user_key_active = False
                        st.session_state.user_ping_msg   = ""
                        st.session_state.user_key_remove_confirm = False
                        st.session_state.messages          = []
                        st.session_state.history           = []
                        st.session_state.summary           = ""
                        st.session_state.first_prompt_sent = False
                        st.session_state.session_initialized = False
                        st.rerun()
                with _rc2:
                    if st.button("Cancel", use_container_width=True,
                                 disabled=_running, key="ownkey_remove_cancel"):
                        st.session_state.user_key_remove_confirm = False
                        st.rerun()
        else:
            # ── Inactive state: key entry form ────────────────────
            st.markdown(
                f"<p style='font-family:\"IBM Plex Mono\",monospace;font-size:11px;"
                f"color:{ts};margin:0 0 10px 0;'>"
                f"The app runs on a default Groq key. Supply your own for higher rate limits.</p>",
                unsafe_allow_html=True,
            )
            _be_choice = st.radio(
                "Backend",
                options=["Groq", "Claude"],
                index=0 if st.session_state.get("user_backend", "groq") == "groq" else 1,
                horizontal=True,
                disabled=_running,
                key="ownkey_backend",
            )
            _chosen_be = "groq" if _be_choice == "Groq" else "claude"
            _ph = ""
            _key_in = st.text_input(
                f"{_be_choice} API key",
                type="password",
                placeholder=_ph,
                value="",
                disabled=_running,
                key="ownkey_input",
            )
            if st.button("Apply", use_container_width=True, type="primary",
                         disabled=_running, key="ownkey_apply"):
                _raw = _key_in.strip()
                if not _raw:
                    st.error("Enter a key first.")
                else:
                    with st.spinner(f"Validating {_be_choice} key…"):
                        try:
                            _r = init_backends(
                                backends=[_chosen_be],
                                secret_names={},
                                test=True,
                                api_keys={_chosen_be: _raw},
                            )
                            st.session_state.user_clients    = _r["clients"]
                            st.session_state.user_backend    = _chosen_be
                            st.session_state.user_key_active = True
                            st.session_state.user_ping_msg   = _r["pings"].get(_chosen_be, "")
                            st.session_state.user_key_remove_confirm = False
                            st.session_state.messages          = []
                            st.session_state.history           = []
                            st.session_state.summary           = ""
                            st.session_state.first_prompt_sent = False
                            st.session_state.session_initialized = False
                            st.rerun()
                        except Exception as _e:
                            _es = str(_e)
                            if any(s in _es for s in ["401", "authentication", "invalid"]):
                                st.error("Authentication failed — check your key.")
                            else:
                                st.error(f"Connection failed: {_es}")

    # ── Spacer pushes Clear/Cancel to bottom ─────────────────────
    st.markdown("<div style='height:18px;'></div>", unsafe_allow_html=True)
    st.divider()

    # ── Cancel / Clear conversation (bottom) ─────────────────────
    if _running:
        if st.button("Cancel request", use_container_width=True, type="primary"):
            rid = st.session_state.get("run_id")
            if rid and rid in _stop_flags:
                _stop_flags[rid].set()
            pre = st.session_state.get("pre_run_mode")
            if pre is not None:
                st.session_state.current_mode = pre
                st.session_state.pending_mode = pre
            st.session_state.is_running    = False
            st.session_state.run_id        = None
            st.session_state.queued_prompt = None
            if st.session_state.messages and st.session_state.messages[-1]["role"] == "user":
                st.session_state.messages.pop()
            st.session_state.first_prompt_sent = len(st.session_state.messages) > 0
            if rid:
                _results.pop(rid, None)
                _stop_flags.pop(rid, None)
            st.rerun()
    elif not st.session_state.confirm_clear:
        if st.button("Clear conversation", use_container_width=True):
            st.session_state.confirm_clear = True
            st.rerun()
    else:
        st.caption("Clear all chat history?")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Yes, clear", use_container_width=True, type="primary"):
                saved = {k: st.session_state[k]
                         for k in ("user_clients", "user_backend", "user_key_active",
                                   "user_ping_msg", "backend", "theme")}
                for k, v in _DEFAULTS.items():
                    st.session_state[k] = v
                for k, v in saved.items():
                    st.session_state[k] = v
                st.session_state.mode_radio = "auto"
                st.rerun()
        with c2:
            if st.button("Cancel", use_container_width=True):
                st.session_state.confirm_clear = False
                st.rerun()

# ═══════════════════════════════════════════════════════════════════════════
# KEY AVAILABILITY CHECK  (graceful error if neither owner key nor user key)
# ═══════════════════════════════════════════════════════════════════════════
_active_clients, _active_backend = _get_active_clients()
if _active_clients is None:
    st.error(
        "No API key configured. "
        "Add `GROQ_API_KEY` to `.streamlit/secrets.toml` (local) or "
        "App settings → Secrets (Streamlit Cloud), then restart."
    )
    st.stop()

# ═══════════════════════════════════════════════════════════════════════════
# DATA PRELOAD + DB WARMUP
# ═══════════════════════════════════════════════════════════════════════════
_start_data_preload()           # non-blocking — downloads CSVs in background thread
_db_logger = _get_db_logger()  # DB ping happens inside; fast after first call

# ── Session + geo init (once per browser session) ────────────────────────
if not st.session_state.session_initialized:
    _sid = str(uuid.uuid4())
    _cid = str(uuid.uuid4())
    st.session_state.session_id  = _sid
    st.session_state.chat_id     = _cid
    st.session_state.turn_num    = 0

    # Geo + device (must run in main thread — accesses st.context.headers)
    _geo  = {}
    _dev  = {}
    try:
        _headers = st.context.headers
        _ip = _headers.get("X-Forwarded-For", "").split(",")[0].strip()
        if not _ip:
            _ip = _headers.get("Remote-Addr", "")
        if _ip:
            _geo = resolve_geo(_ip)
        _ua_str = _headers.get("User-Agent", "")
        _dev = parse_user_agent(_ua_str)
    except Exception:
        pass

    _now = datetime.now(tz=timezone.utc)
    threading.Thread(target=_db_logger.create_session, daemon=True, kwargs={
        "session_id": _sid,
        "started_at": _now,
        "timezone":     _geo.get("timezone"),
        "city":         _geo.get("city"),
        "region":       _geo.get("region"),
        "country":      _geo.get("country"),
        "country_code": _geo.get("country_code"),
        "device_type":  _dev.get("device_type"),
        "os":           _dev.get("os"),
        "browser":      _dev.get("browser"),
    }).start()
    threading.Thread(target=_db_logger.create_chat, daemon=True, kwargs={
        "chat_id":    _cid,
        "session_id": _sid,
        "backend":    _active_backend or "groq",
        "started_at": _now,
    }).start()

    st.session_state.session_initialized = True


# ═══════════════════════════════════════════════════════════════════════════
# AGENT THREAD FUNCTION
# ═══════════════════════════════════════════════════════════════════════════

def _run_agent(
    run_id: str,
    prompt: str,
    history: list,
    summary: str,
    turn_count: int,
    backend: str,
    clients: dict,
    current_mode: int,
    mode_control: str,
    stop_ev: threading.Event,
):
    """Full agent pipeline: parse → interpret → classify+plan+execute+narrate → summary."""

    def check():
        if stop_ev.is_set():
            raise InterruptedError()

    t_turn_start = time.time()

    try:
        # ── Get data (returns from cache instantly if preload finished; waits if still loading) ──
        schema = _load_schema()
        tables = _load_data()

        # ── Build closures ──
        def llm_fn(p, tier=1):
            return call_llm(p, backend, clients, model=tier)

        def execute_fn(q, t):
            return execute_queries(q, t)

        def agent_fn(question, mode=0, user_context=None):
            return analyst_agent(question, schema, tables, llm_fn, execute_fn, mode, user_context)

        # ── Stage 0: parse slash command ──
        question, cmd = parse_mode_command(prompt)
        if cmd == "auto":
            mode_control = "auto"
        elif cmd is not None and cmd in CMD_TO_MODE:
            current_mode = CMD_TO_MODE[cmd]
            mode_control = "manual"

        # Signal: interpreting
        _results[run_id]["stage"] = "interpreting"
        check()

        # ── Stage 1: interpret turn ──
        last_turn = ""
        if history:
            h = history[-1]
            last_turn = f"User: {h['question']}\nBot: {h['narrative']}"

        resolved, suggested_mode, _mode_reason, turn_type, interp_record = interpret_turn(
            question=question,
            summary=summary,
            last_turn=last_turn,
            turn_count=turn_count,
            current_mode=current_mode,
            mode_control=mode_control,
            llm_fn=llm_fn,
        )
        turn_trace = [interp_record]

        check()

        # ── Resolve active mode ──
        prev_mode = _results[run_id].get("prev_mode", current_mode)
        if mode_control == "manual":
            active_mode = current_mode
        else:
            active_mode = suggested_mode if suggested_mode is not None else current_mode

        transition = _TRANSITIONS.get((prev_mode, active_mode)) if active_mode != prev_mode else None

        # Signal: resolved (publish intermediate info for the UI)
        _mode_label = _MODE_NAMES[active_mode] if isinstance(active_mode, int) and 0 <= active_mode < len(_MODE_NAMES) else str(active_mode)
        if active_mode != prev_mode:
            _mode_step = f"Switching to {_mode_label} mode."
        else:
            _mode_step = f"Using {_mode_label} mode."
        _results[run_id].update({
            "stage":          "planning",
            "stage_resolved": resolved,
            "stage_turn_type": turn_type,
            "stage_mode_step": _mode_step,
            "active_mode":    active_mode,
            "mode_control":   mode_control,   # needed by polling loop to update radio
        })
        check()

        # ── Stages 2+3: classify + plan + execute + narrate ──
        user_context = extract_user_context(resolved)
        r = agent_fn(resolved, mode=active_mode, user_context=user_context)
        turn_trace.extend(r.get("stage_trace", []))

        check()

        # ── Stage 5: update rolling summary ──
        new_summary, summ_record = update_summary(
            summary, question, r["narrative"], llm_fn
        )
        if summ_record:
            turn_trace.append(summ_record)

        enriched_trace = enrich_trace(turn_trace, backend)
        total_sec      = round(time.time() - t_turn_start, 2)

        _results[run_id] = {
            **_results.get(run_id, {}),
            "status":       "done",
            "prompt":       prompt,
            "question":     question,
            "resolved":     resolved,
            "turn_type":    turn_type,
            "active_mode":  active_mode,
            "mode_control": mode_control,
            "transition":   transition,
            "reply":        r["narrative"].replace("$", r"\$"),
            "narrative_raw": r["narrative"],
            "queries":      r.get("queries", []),
            "sql":          r.get("code", ""),
            "classification": r.get("classification", ""),
            "stage_trace":  enriched_trace,
            "total_seconds": total_sec,
            "new_summary":  new_summary,
            "error":        None,
        }

    except InterruptedError:
        _results[run_id] = {**_results.get(run_id, {}), "status": "stopped"}

    except Exception as _exc:
        _err = str(_exc)
        if any(s in _err for s in ["401", "authentication", "invalid x-api-key", "api key"]):
            _reply = "Authentication failed — please check your API key in the sidebar."
        elif any(s in _err for s in ["413", "rate_limit_exceeded", "tokens", "TPM", "RPM", "quota", "limit", "too large", "reduce your message"]):
            _reply = "Consumption limit reached. Please try again later, or use your own LLM key via the sidebar."
        else:
            _reply = f"Something went wrong: {_err}"
        _results[run_id] = {
            **_results.get(run_id, {}),
            "status":       "error",
            "reply":        _reply,
            "narrative_raw": _reply,
            "resolved":     prompt,
            "active_mode":  current_mode,
            "error":        _err,
            "total_seconds": round(time.time() - t_turn_start, 2),
        }


# ═══════════════════════════════════════════════════════════════════════════
# DB LOGGING HELPER  (called from daemon threads — never blocks the UI)
# ═══════════════════════════════════════════════════════════════════════════

def _async_log_turn(logger, turn_id, result, session_id, chat_id, turn_num):
    try:
        logger.log_turn(
            turn_id, chat_id, session_id,
            turn_num=turn_num,
            question_raw=result.get("prompt", ""),
            question_resolved=result.get("resolved", ""),
            mode=result.get("active_mode", 0),
            mode_control=result.get("mode_control", "auto"),
            turn_type=result.get("turn_type"),
            classification=result.get("classification", ""),
            total_seconds=result.get("total_seconds", 0.0),
            turn_status=result.get("status", "done"),
            error=result.get("error"),
            narrative=result.get("narrative_raw", ""),
            raw_output={k: v for k, v in result.items()
                        if k not in ("queries", "reply", "narrative_raw")},
        )
        logger.log_stage_traces(turn_id, result.get("stage_trace", []))
        logger.log_queries(turn_id, result.get("queries", []))
        logger.update_chat(chat_id, turn_count_delta=1, mode=result.get("active_mode"))
        logger.update_session(
            session_id,
            turns_delta=1,
            mode=result.get("active_mode"),
            backend=result.get("backend"),
        )
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════
# MAIN CHAT AREA
# ═══════════════════════════════════════════════════════════════════════════
with st.container(key="chat_area"):

    # ── Suggestion cards (first load) ─────────────────────────
    if not st.session_state.first_prompt_sent and not _running:
        st.markdown(
            f"<div style='max-width:660px;margin:20px auto 10px;text-align:center;'>"
            f"<p style='color:{ts};font-size:0.88rem;margin-bottom:10px;'>Try asking:</p></div>",
            unsafe_allow_html=True,
        )
        _sug_cols = st.columns(len(_SUGGESTIONS))
        for _i, _s in enumerate(_SUGGESTIONS):
            with _sug_cols[_i]:
                if st.button(_s, key=f"sug_{_i}", use_container_width=True):
                    st.session_state.first_prompt_sent = True
                    st.session_state.pre_run_mode      = _ci
                    st.session_state.queued_prompt     = _s
                    st.session_state.messages.append({"role": "user", "content": _s})
                    st.session_state.is_running        = True
                    st.rerun()

    # ── Message history ───────────────────────────────────────
    for _msg in st.session_state.messages:
        with st.chat_message(_msg["role"]):
            # Pipeline trace (assistant messages only, persisted from run)
            if _msg["role"] == "assistant" and _msg.get("pipeline_html"):
                st.markdown(_msg["pipeline_html"], unsafe_allow_html=True)
            st.markdown(_msg["content"])
            # SQL expander + elapsed time
            if _msg["role"] == "assistant":
                _msg_sql  = _msg.get("sql", "")
                _msg_secs = _msg.get("total_seconds")
                _time_label = f"  ·  ⏱ {_msg_secs:.1f} s" if _msg_secs else ""
                if _msg_sql:
                    with st.expander(f"Show SQL{_time_label}", expanded=False):
                        st.code(_msg_sql, language="sql")
                elif _msg_secs:
                    st.markdown(
                        f"<div style='margin-top:4px;font-family:\"IBM Plex Mono\",monospace;"
                        f"font-size:0.72rem;color:{ts};'>⏱ {_msg_secs:.1f} s</div>",
                        unsafe_allow_html=True,
                    )

    # ── Agent thread: launch ──────────────────────────────────
    if st.session_state.is_running and st.session_state.run_id is None and st.session_state.queued_prompt:
        _rid    = str(uuid.uuid4())
        _prompt = st.session_state.queued_prompt
        st.session_state.queued_prompt = None
        st.session_state.run_id        = _rid

        _stop_ev = threading.Event()
        _stop_flags[_rid] = _stop_ev
        _results[_rid]    = {
            "status":    "running",
            "prev_mode": _ci,
            "transition": None,
            "prompt":    _prompt,
        }

        _t_clients, _t_backend = _get_active_clients()
        threading.Thread(
            target=_run_agent,
            daemon=True,
            args=(
                _rid, _prompt,
                list(st.session_state.history),
                st.session_state.summary,
                st.session_state.turn_num,
                _t_backend,
                _t_clients,
                st.session_state.current_mode,
                st.session_state.mode_control,
                _stop_ev,
            ),
        ).start()

    # ── Agent thread: poll ────────────────────────────────────
    if st.session_state.is_running:
        _rid = st.session_state.run_id
        if _rid is not None:
            _res = _results.get(_rid)

            if _res is None:
                # Hot-reload: lost thread state — clean up
                st.session_state.run_id        = None
                st.session_state.is_running    = False
                st.session_state.queued_prompt = None
                if st.session_state.messages and st.session_state.messages[-1]["role"] == "user":
                    st.session_state.messages.pop()
                st.session_state.first_prompt_sent = len(st.session_state.messages) > 0
                st.rerun()

            _status = _res.get("status", "running")
            _ni     = _res.get("active_mode", _ci)

            if _status == "running":
                _stage        = _res.get("stage", "interpreting")
                _s_resolved   = _res.get("stage_resolved", "")
                _s_turn_type  = _res.get("stage_turn_type", "")
                _s_mode_step  = _res.get("stage_mode_step", "")
                _s_active_mode= _res.get("active_mode")

                # As soon as the thread resolves the mode, reflect it in the sidebar.
                # Always update — slash commands may change mode_control even if
                # active_mode number stays the same (e.g. auto→manual on same mode).
                if _stage == "planning" and _s_active_mode is not None:
                    _res_mc = _res.get("mode_control", st.session_state.mode_control)
                    st.session_state.current_mode = _s_active_mode
                    st.session_state.pending_mode = _s_active_mode
                    st.session_state.mode_control = _res_mc
                    if _res_mc == "auto":
                        st.session_state.mode_radio = "auto"
                    elif 0 <= _s_active_mode <= 2:
                        st.session_state.mode_radio = ["retrieve", "explore", "reason"][_s_active_mode]

                # Build pipeline step HTML
                _arrow = f"<span class='step-arrow'>→</span>"
                _lines = [
                    f"<div>{_arrow}<span class='step-label'>Interpreting…</span></div>"
                ]
                if _stage == "planning" and _s_resolved:
                    _lines.append(
                        f"<div class='step-sub'>{_arrow}"
                        f"<span class='step-label'>Resolved as: </span>"
                        f"<span class='step-value'>{_s_resolved}</span></div>"
                    )
                    if _s_turn_type:
                        _lines.append(
                            f"<div class='step-sub'>{_arrow}"
                            f"<span class='step-label'>Interaction type: </span>"
                            f"<span class='step-value'>{_s_turn_type}</span></div>"
                        )
                    if _s_mode_step:
                        _lines.append(
                            f"<div class='step-sub'>{_arrow}"
                            f"<span class='step-label'>Mode: </span>"
                            f"<span class='step-value'>{_s_mode_step}</span></div>"
                        )
                    _lines.append(
                        f"<div>{_arrow}<span class='step-label'>Planning and executing queries…</span></div>"
                    )

                _pipeline_html = f"<div class='pipeline-steps'>{''.join(_lines)}</div>"

                with st.chat_message("assistant"):
                    st.markdown(_pipeline_html, unsafe_allow_html=True)
                time.sleep(0.35)
                st.rerun()

            elif _status in ("done", "error"):
                _reply      = _res.get("reply", "")
                # Sanitise error strings that bubble up inside the narrative
                if _reply and any(s in _reply for s in ["Narrative failed:", "Classify/plan failed:"]):
                    _inner = _reply.split(":", 1)[-1].strip() if ":" in _reply else _reply
                    if any(s in _inner for s in ["413", "rate_limit_exceeded", "tokens", "TPM", "RPM", "quota", "too large", "reduce your message"]):
                        _reply = "Consumption limit reached. Please try again in a moment, or use your own LLM key via the sidebar."
                    elif any(s in _inner for s in ["401", "authentication", "invalid x-api-key", "api key"]):
                        _reply = "Authentication failed — please check your API key in the sidebar."
                    else:
                        _reply = "Something went wrong on the server. Please try again."
                _sql        = _res.get("sql", "")
                _transition = _res.get("transition")
                _active_mode= _res.get("active_mode", _ci)
                _new_summary= _res.get("new_summary", st.session_state.summary)

                # Build the full (completed) pipeline steps to persist in history
                _arrow = f"<span class='step-arrow'>→</span>"
                _done_lines = [f"<div>{_arrow}<span class='step-label'>Interpreting…</span></div>"]
                _s_resolved  = _res.get("stage_resolved", "")
                _s_turn_type = _res.get("stage_turn_type", "")
                _s_mode_step = _res.get("stage_mode_step", "")
                if _s_resolved:
                    _done_lines.append(
                        f"<div class='step-sub'>{_arrow}<span class='step-label'>Resolved as: </span>"
                        f"<span class='step-value'>{_s_resolved}</span></div>"
                    )
                if _s_turn_type:
                    _done_lines.append(
                        f"<div class='step-sub'>{_arrow}<span class='step-label'>Interaction type: </span>"
                        f"<span class='step-value'>{_s_turn_type}</span></div>"
                    )
                if _s_mode_step:
                    _done_lines.append(
                        f"<div class='step-sub'>{_arrow}<span class='step-label'>Mode: </span>"
                        f"<span class='step-value'>{_s_mode_step}</span></div>"
                    )
                _done_lines.append(
                    f"<div>{_arrow}<span class='step-label'>Planning and executing queries…</span></div>"
                )
                _pipeline_html = f"<div class='pipeline-steps'>{''.join(_done_lines)}</div>"

                _total_secs = _res.get("total_seconds")
                _time_str   = f"&nbsp;&nbsp;<span style='font-family:\"IBM Plex Mono\",monospace;font-size:0.72rem;color:{ts};'>⏱ {_total_secs:.1f} s</span>" if _total_secs else ""

                with st.chat_message("assistant"):
                    st.markdown(_pipeline_html, unsafe_allow_html=True)
                    st.markdown(_reply)
                    _time_label = f"  ·  ⏱ {_total_secs:.1f} s" if _total_secs else ""
                    if _sql:
                        with st.expander(f"Show SQL{_time_label}", expanded=False):
                            st.code(_sql, language="sql")
                    elif _total_secs:
                        st.markdown(
                            f"<div style='margin-top:4px;font-family:\"IBM Plex Mono\",monospace;"
                            f"font-size:0.72rem;color:{ts};'>⏱ {_total_secs:.1f} s</div>",
                            unsafe_allow_html=True,
                        )

                # Update state
                st.session_state.current_mode  = _active_mode
                st.session_state.pending_mode  = _active_mode
                st.session_state.summary       = _new_summary
                st.session_state.turn_num     += 1
                # Sync mode_radio (slash commands may have changed mode/control)
                _res_mc = _res.get("mode_control", st.session_state.mode_control)
                st.session_state.mode_control = _res_mc
                if _res_mc == "auto":
                    st.session_state.mode_radio = "auto"
                elif 0 <= _active_mode <= 2:
                    st.session_state.mode_radio = ["retrieve", "explore", "reason"][_active_mode]

                # Append to display messages (include pipeline steps for re-render)
                st.session_state.messages.append({
                    "role":          "assistant",
                    "content":       _reply,
                    "sql":           _sql,
                    "mode":          _active_mode,
                    "pipeline_html": _pipeline_html,
                    "total_seconds": _total_secs,
                })

                # Append to history (used by interpret_turn next turn)
                st.session_state.history.append({
                    "question":       _res.get("prompt", ""),
                    "resolved":       _res.get("resolved", ""),
                    "narrative":      _res.get("narrative_raw", ""),
                    "mode":           _active_mode,
                    "classification": _res.get("classification", ""),
                })

                # DB log (daemon thread — never blocks)
                _turn_id = str(uuid.uuid4())
                _, _log_backend = _get_active_clients()
                threading.Thread(
                    target=_async_log_turn,
                    daemon=True,
                    args=(_db_logger, _turn_id, {**_res, "backend": _log_backend or "groq"},
                          st.session_state.session_id, st.session_state.chat_id,
                          st.session_state.turn_num),
                ).start()

                # Clean up thread state
                st.session_state.is_running = False
                st.session_state.run_id     = None
                _results.pop(_rid, None)
                _stop_flags.pop(_rid, None)
                st.rerun()

            elif _status == "stopped":
                st.session_state.is_running = False
                st.session_state.run_id     = None
                if st.session_state.messages and st.session_state.messages[-1]["role"] == "user":
                    st.session_state.messages.pop()
                st.session_state.first_prompt_sent = len(st.session_state.messages) > 0
                _results.pop(_rid, None)
                _stop_flags.pop(_rid, None)
                st.rerun()

# ═══════════════════════════════════════════════════════════════════════════
# CHAT INPUT
# ═══════════════════════════════════════════════════════════════════════════
if _prompt_in := st.chat_input(
    "Ask a question about the data…",
    disabled=_running,
):
    _prompt_stripped = _prompt_in.strip()

    # ── Ignore blank / whitespace-only submissions ──────────────────────────
    if not _prompt_stripped:
        pass  # do nothing

    else:
        _q_part, _cmd = parse_mode_command(_prompt_stripped)

        # ── Slash-only command (no question body) ───────────────────────────
        # Just update mode state + show a minimal ack; no LLM call.
        if _cmd is not None and not _q_part.strip():
            # Update mode state immediately
            if _cmd == "auto":
                st.session_state.mode_control = "auto"
                st.session_state.mode_radio   = "auto"
                _slash_mode_step = "Using Auto mode — agent will pick the best mode each turn."
            elif _cmd in CMD_TO_MODE:
                _nm = CMD_TO_MODE[_cmd]
                st.session_state.mode_control = "manual"
                st.session_state.current_mode = _nm
                st.session_state.mode_radio   = ["retrieve", "explore", "reason"][_nm]
                _prev_nm = _ci  # mode before this command
                if _nm != _prev_nm:
                    _slash_mode_step = f"Switching to {_MODE_NAMES[_nm]} mode."
                else:
                    _slash_mode_step = f"Using {_MODE_NAMES[_nm]} mode."
            else:
                _slash_mode_step = ""

            # Build minimal pipeline display (Interpreting → Mode only)
            _sarrow = "<span class='step-arrow'>→</span>"
            _slines = [f"<div>{_sarrow}<span class='step-label'>Interpreting…</span></div>"]
            if _slash_mode_step:
                _slines.append(
                    f"<div class='step-sub'>{_sarrow}"
                    f"<span class='step-label'>Mode: </span>"
                    f"<span class='step-value'>{_slash_mode_step}</span></div>"
                )
            _slash_pipeline = f"<div class='pipeline-steps'>{''.join(_slines)}</div>"

            # Add user message + minimal bot acknowledgement (no content, no SQL)
            st.session_state.first_prompt_sent = True
            st.session_state.messages.append({"role": "user", "content": _prompt_stripped})
            st.session_state.messages.append({
                "role":          "assistant",
                "content":       "",
                "sql":           "",
                "mode":          st.session_state.current_mode,
                "pipeline_html": _slash_pipeline,
                "total_seconds": None,
            })
            st.rerun()

        # ── Normal prompt (question with or without a leading slash command) ─
        else:
            st.session_state.first_prompt_sent = True
            st.session_state.pre_run_mode      = _ci
            st.session_state.messages.append({"role": "user", "content": _prompt_stripped})
            st.session_state.queued_prompt     = _prompt_stripped
            st.session_state.is_running        = True
            st.rerun()

# (Slash-command hint is injected into stBottom via JS below)

# ═══════════════════════════════════════════════════════════════════════════
# JS: slider track color fix + auto-scroll + scroll-down button
# Runs in an iframe — accesses window.parent.document freely
# ═══════════════════════════════════════════════════════════════════════════
st.components.v1.html(f"""
<script>
var D = window.parent.document;

/* ── Fix slider track colors ── */
function fixSlider() {{
  var slider = D.querySelector('section[data-testid="stSidebar"] [data-testid="stSlider"]');
  if (!slider) return;
  slider.querySelectorAll('div').forEach(function(d) {{
    if (d.getAttribute('role') === 'slider') return;
    d.style.setProperty('background', 'transparent', 'important');
    d.style.setProperty('background-color', 'transparent', 'important');
  }});
  slider.querySelectorAll('div').forEach(function(d) {{
    var h = d.offsetHeight, w = d.offsetWidth;
    if (h >= 2 && h <= 5 && w > 20 && d.getAttribute('role') !== 'slider') {{
      d.style.setProperty('background', '{track_col}', 'important');
      d.style.setProperty('background-color', '{track_col}', 'important');
    }}
  }});
  var knob = slider.querySelector('[role="slider"]');
  if (knob) {{
    knob.style.setProperty('background', '{knob_col}', 'important');
    knob.style.setProperty('background-color', '{knob_col}', 'important');
    knob.style.setProperty('border', 'none', 'important');
  }}
}}
fixSlider();
setInterval(fixSlider, 400);

/* ── Suppress Chrome password manager on API key field ──
   Changing type→text defeats Chrome's heuristic; CSS -webkit-text-security
   keeps the characters visually masked (dots), same UX as type=password. */
function fixAutocomplete() {{
  D.querySelectorAll('input[type="password"]').forEach(function(el) {{
    if (el.getAttribute('data-pw-fixed')) return;
    el.setAttribute('type', 'text');
    el.setAttribute('autocomplete', 'off');
    el.setAttribute('data-lpignore', 'true');
    el.setAttribute('data-form-type', 'other');
    el.style.setProperty('-webkit-text-security', 'disc', 'important');
    el.setAttribute('data-pw-fixed', '1');

    /* Enter key → click the Apply button in the sidebar */
    el.addEventListener('keydown', function(e) {{
      if (e.key !== 'Enter') return;
      var sidebar = D.querySelector('section[data-testid="stSidebar"]');
      if (!sidebar) return;
      var btns = sidebar.querySelectorAll('button');
      for (var i = 0; i < btns.length; i++) {{
        if (btns[i].innerText.trim() === 'Apply') {{
          btns[i].click();
          break;
        }}
      }}
    }});
  }});
}}
fixAutocomplete();
setInterval(fixAutocomplete, 600);

/* ── Scroller helper ── */
function getScroller() {{
  var m = D.querySelector('[data-testid="stMainBlockContainer"]');
  if (m) {{
    var p = m.parentElement;
    while (p) {{ if (p.scrollHeight > p.clientHeight + 50) return p; p = p.parentElement; }}
  }}
  return D.documentElement;
}}

/* ── Auto-scroll on new reply ── */
function scrollDown() {{ getScroller().scrollTop = 99999; }}
scrollDown(); setTimeout(scrollDown, 200); setTimeout(scrollDown, 800);

function scrollDownSmooth() {{
  var s = getScroller();
  if (!s) return;
  (typeof s.scrollTo === 'function')
    ? s.scrollTo({{ top: s.scrollHeight, behavior: 'smooth' }})
    : (s.scrollTop = s.scrollHeight);
}}

/* ── Scroll-down floating button ── */
var btn = D.getElementById('sdb');
if (!btn) {{
  btn = D.createElement('button');
  btn.id = 'sdb';
  btn.innerHTML = '&#x2193;';
  D.body.appendChild(btn);
}}
btn.onclick = function() {{ scrollDownSmooth(); }};
btn.style.cssText = [
  'position:fixed', 'bottom:90px',
  'left:calc(300px + (100vw - 300px) / 2)', 'transform:translateX(-50%)',
  'z-index:9999', 'width:36px', 'height:36px', 'border-radius:50%',
  'background:{bg2}', 'color:{tp}', 'border:1px solid {border}',
  'cursor:pointer', 'font-size:18px', 'line-height:36px', 'text-align:center',
  'box-shadow:0 2px 8px rgba(0,0,0,.18)', 'display:none', 'transition:opacity .15s'
].join(';');

function checkBtn() {{
  var s = getScroller();
  btn.style.display = (s.scrollHeight - s.scrollTop - s.clientHeight > 150) ? 'block' : 'none';
}}
getScroller().addEventListener('scroll', checkBtn, {{passive:true}});
setInterval(checkBtn, 1000);
checkBtn();

</script>
""", height=0)
