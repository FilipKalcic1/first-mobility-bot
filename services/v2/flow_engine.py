"""L3 — Flow Engine.

A flow is a multi-turn interaction that collects parameters from the
user step-by-step, then issues exactly ONE mutation API call (preceded
by an explicit Da/Ne confirm dialog).

Why flows live separately from L2 quick-path:
  - L2 returns a plan immediately — single-turn, no state
  - L3 needs state across user messages (Redis-backed)
  - L3 is the *only* layer that can issue mutations from L2 dispatch
  - All mutations route through L3 → L6 (confirm) → L7 (execute)

Design:
  - Each flow is declared as a list of STEPS (data, not code)
  - Each step has: prompt, validator, slot_name, optional choices
  - Engine is a single state machine that walks the steps
  - State is serializable JSON — survives Redis SETEX, restarts, etc.
  - Cancellable at any time ("odustani"/"ne"/"prekini")
  - Timeout in Redis (10 minutes default) — abandoned flows auto-clear

Step kinds:
  ASK_TEXT     — free text response (description, comment)
  ASK_NUMBER   — numeric value (mileage)
  ASK_PERIOD   — datetime range (booking from/to)
  ASK_CHOICE   — pick from list (which vehicle, which trip)
  ASK_CONFIRM  — Da/Ne (the L6 mutation gate, always last)
  EXEC_LOOKUP  — call a read API to populate choices for next step
                 (e.g. get_AvailableVehicles before ASK_CHOICE)

The flow definitions for booking/mileage/case live below as data, not
hardcoded handlers. Adding a new flow = adding one entry to FLOWS.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo

# Resolve relative dates against Croatia, not the host TZ (prod may be UTC).
try:
    _ZAGREB = ZoneInfo("Europe/Zagreb")
except Exception:  # noqa: BLE001
    _ZAGREB = None
from typing import Any, Callable, Optional

# Reuse the single Da/Ne classifier so flow confirms parse IDENTICALLY to the
# general [C] path (strict execute, lenient cancel, "može biti"→ambiguous).
from services.v2.pending_mutation import STAGE_SINGLE, parse_reply

logger = logging.getLogger(__name__)


# Default flow timeout — abandoned flows auto-clear from Redis after this.
FLOW_TTL_SECONDS = 600  # 10 minutes

# Redis key prefix for flow state.
_REDIS_PREFIX = "v2:flow:"


# Step kinds (constants — not enum to keep JSON-friendly).
STEP_ASK_TEXT    = "ask_text"
STEP_ASK_NUMBER  = "ask_number"
STEP_ASK_PERIOD  = "ask_period"
STEP_ASK_CHOICE  = "ask_choice"
STEP_ASK_CONFIRM = "ask_confirm"
STEP_EXEC_LOOKUP = "exec_lookup"


# Engine outcome kinds — what to do with the user-facing response.
OUTCOME_PROMPT     = "prompt"      # bot asks user something
OUTCOME_EXECUTE    = "execute"     # confirmed; caller runs final API call
OUTCOME_CANCELLED  = "cancelled"   # user cancelled; clear flow state
OUTCOME_INVALID    = "invalid"     # input failed validation; re-ask
OUTCOME_DONE       = "done"        # flow finished successfully


# --------------------------------------------------------------------------
# Data structures
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    """One node in a flow's state machine.

    `kind` decides which validator / behavior runs.
    `slot_name` is where the captured value gets stored in collected_params.
    `prompt` is the bot's question (Croatian, with template placeholders).
    For ASK_CHOICE, `choices_slot` points to the list to pick from
    (populated by an earlier EXEC_LOOKUP step).
    For EXEC_LOOKUP, `lookup_tool_id` and `lookup_params_template` define
    the read call.
    """
    kind: str
    slot_name: str
    prompt: str = ""
    choices_slot: Optional[str] = None
    lookup_tool_id: Optional[str] = None
    lookup_params_template: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Flow:
    name: str
    steps: tuple[Step, ...]
    final_tool_id: str          # mutation tool to call after confirm
    final_params_builder: Callable[[dict], dict]
    """Takes collected_params dict, returns API call params."""


@dataclass
class FlowState:
    flow_name: str
    step_index: int = 0
    collected_params: dict = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps({
            "flow_name": self.flow_name,
            "step_index": self.step_index,
            "collected_params": self.collected_params,
            "started_at": self.started_at,
        })

    @classmethod
    def from_json(cls, raw: str) -> "FlowState":
        data = json.loads(raw)
        return cls(
            flow_name=data["flow_name"],
            step_index=int(data.get("step_index", 0)),
            collected_params=dict(data.get("collected_params", {})),
            started_at=float(data.get("started_at", time.time())),
        )


@dataclass(frozen=True)
class FlowOutcome:
    kind: str                                  # OUTCOME_*
    response: Optional[str] = None             # text to send user
    tool_id: Optional[str] = None              # populated for EXECUTE
    params: dict = field(default_factory=dict) # populated for EXECUTE
    new_state: Optional[FlowState] = None      # None means clear


# --------------------------------------------------------------------------
# Flow Engine — pure logic, IO is up to caller (Redis + Gateway)
# --------------------------------------------------------------------------


class FlowEngine:
    """State-machine driver. Pure functions over flow + state + input.

    Caller responsibilities:
      - Read state from Redis before calling
      - Persist new_state to Redis after, OR delete on cancelled/done
      - Execute lookup_tool / final_tool API calls when engine asks

    The engine itself does NOT touch Redis or the API gateway. This
    makes it 100% unit-testable without infra.
    """

    def __init__(self, flows: dict[str, Flow]):
        self._flows = flows

    def start(
        self,
        flow_name: str,
        identity_context: dict,
    ) -> FlowOutcome:
        """Begin a new flow. Returns OUTCOME_PROMPT with first question.

        identity_context: dict with at minimum person_id, vehicle_id,
        vehicle_name — used as initial collected_params seed.
        """
        flow = self._flows.get(flow_name)
        if flow is None:
            return FlowOutcome(
                kind=OUTCOME_CANCELLED,
                response=f"Nepoznat flow: {flow_name}.",
                new_state=None,
            )

        state = FlowState(
            flow_name=flow_name,
            step_index=0,
            collected_params=dict(identity_context),
        )
        return self._advance_or_prompt(flow, state)

    def handle(
        self,
        state: FlowState,
        user_input: str,
        lookup_result: Optional[Any] = None,
    ) -> FlowOutcome:
        """Process one user message OR one lookup result.

        If the current step is EXEC_LOOKUP and `lookup_result` is None,
        the engine returns OUTCOME_PROMPT-with-tool_id (signaling the
        caller to run that read call and call back). When caller calls
        back with `lookup_result`, engine stores it and advances.

        Cancellation triggers (matched on the current user_input, never
        on lookup_result):
          'odustani', 'prekini', 'cancel', 'ne'
        """
        flow = self._flows.get(state.flow_name)
        if flow is None:
            return FlowOutcome(
                kind=OUTCOME_CANCELLED,
                response="Flow je nestao iz konfiguracije. Probaj ponovo.",
                new_state=None,
            )

        if state.step_index >= len(flow.steps):
            # Past the end — unusual; treat as done
            return FlowOutcome(kind=OUTCOME_DONE, new_state=None)

        current = flow.steps[state.step_index]

        # User-cancellation only valid when waiting on user input.
        if current.kind != STEP_EXEC_LOOKUP and _is_cancel(user_input):
            return FlowOutcome(
                kind=OUTCOME_CANCELLED,
                response="U redu, odustao sam od postupka.",
                new_state=None,
            )

        # Branch by step kind.
        if current.kind == STEP_EXEC_LOOKUP:
            return self._handle_lookup(flow, state, current, lookup_result)

        if current.kind == STEP_ASK_CONFIRM:
            return self._handle_confirm(flow, state, current, user_input)

        # ASK_TEXT / ASK_NUMBER / ASK_PERIOD / ASK_CHOICE — capture value.
        validated = _validate(current, user_input, state.collected_params)
        if validated is None:
            return FlowOutcome(
                kind=OUTCOME_INVALID,
                response=_invalid_message(current),
                new_state=state,  # preserve state for retry
            )

        # ASK_PERIOD returns a dict with multiple slot values (period_text,
        # from_time, to_time). Merge all keys into collected_params.
        if isinstance(validated, dict) and current.kind == STEP_ASK_PERIOD:
            merged = {**state.collected_params, **validated}
        else:
            merged = {**state.collected_params, current.slot_name: validated}

        new_state = FlowState(
            flow_name=state.flow_name,
            step_index=state.step_index + 1,
            collected_params=merged,
            started_at=state.started_at,
        )
        return self._advance_or_prompt(flow, new_state)

    # ---- internals -------------------------------------------------------

    def _advance_or_prompt(self, flow: Flow, state: FlowState) -> FlowOutcome:
        """If current step is EXEC_LOOKUP, return tool_id so caller fetches.
        Else build the user-facing prompt for the current step.

        HIGH-1 fix (Filip 2026-05-28): skip ASK_* steps whose slot is already
        populated (pre-filled from initial query by _start_flow). For booking
        ASK_PERIOD, "from_time" + "to_time" together are the effective slot
        (period_text is the optional textual mirror). Bounded by len(steps).
        """
        # Auto-skip already-populated ASK_* steps (pre-fill from initial query)
        for _skip_guard in range(len(flow.steps) + 1):
            if state.step_index >= len(flow.steps):
                return FlowOutcome(kind=OUTCOME_DONE, new_state=None)
            _step = flow.steps[state.step_index]
            if _step.kind == STEP_ASK_PERIOD:
                # Period is fully resolved iff BOTH datetimes are present
                if (state.collected_params.get("from_time")
                        and state.collected_params.get("to_time")):
                    state = FlowState(
                        flow_name=state.flow_name,
                        step_index=state.step_index + 1,
                        collected_params=state.collected_params,
                        started_at=state.started_at,
                    )
                    continue
            elif _step.kind in (STEP_ASK_NUMBER, STEP_ASK_TEXT):
                slot = _step.slot_name
                v = state.collected_params.get(slot) if slot else None
                if v not in (None, ""):
                    state = FlowState(
                        flow_name=state.flow_name,
                        step_index=state.step_index + 1,
                        collected_params=state.collected_params,
                        started_at=state.started_at,
                    )
                    continue
            break

        if state.step_index >= len(flow.steps):
            # No more steps — done
            return FlowOutcome(kind=OUTCOME_DONE, new_state=None)

        step = flow.steps[state.step_index]
        if step.kind == STEP_EXEC_LOOKUP:
            params = _resolve_template(
                step.lookup_params_template, state.collected_params
            )
            return FlowOutcome(
                kind=OUTCOME_PROMPT,
                response=None,  # no user-facing prompt; caller runs API
                tool_id=step.lookup_tool_id,
                params=params,
                new_state=state,
            )

        prompt = _render_prompt(step.prompt, state.collected_params)
        if step.kind == STEP_ASK_CHOICE:
            choices = state.collected_params.get(step.choices_slot or "") or []
            prompt = _render_choices(prompt, choices)

        return FlowOutcome(
            kind=OUTCOME_PROMPT, response=prompt, new_state=state
        )

    def _handle_lookup(
        self, flow: Flow, state: FlowState, step: Step, lookup_result: Any
    ) -> FlowOutcome:
        """Caller has performed the read call; store result + advance."""
        if lookup_result is None:
            # Caller hasn't called API yet — re-emit the lookup prompt.
            return self._advance_or_prompt(flow, state)
        new_state = FlowState(
            flow_name=state.flow_name,
            step_index=state.step_index + 1,
            collected_params={
                **state.collected_params, step.slot_name: lookup_result
            },
            started_at=state.started_at,
        )
        return self._advance_or_prompt(flow, new_state)

    def _handle_confirm(
        self, flow: Flow, state: FlowState, step: Step, user_input: str
    ) -> FlowOutcome:
        """Final Da/Ne. Da → EXECUTE, Ne → CANCELLED, else re-ask.

        Uses the shared `parse_reply` so flow confirms behave EXACTLY like the
        general [C] confirm: strict execute (exact "da"/"može"/…), lenient
        cancel ("ne hvala" still cancels), hesitant ("može biti") → re-ask.
        """
        action = parse_reply(user_input, STAGE_SINGLE)
        if action == "execute":
            params = flow.final_params_builder(state.collected_params)
            return FlowOutcome(
                kind=OUTCOME_EXECUTE,
                tool_id=flow.final_tool_id,
                params=params,
                new_state=None,  # caller clears state after executing
            )
        if action == "cancel":
            return FlowOutcome(
                kind=OUTCOME_CANCELLED,
                response="U redu, odustao sam.",
                new_state=None,
            )
        # Ambiguous / hesitant input at confirm step — re-ask.
        return FlowOutcome(
            kind=OUTCOME_INVALID,
            response="Molim te odgovori sa 'Da' ili 'Ne'.",
            new_state=state,
        )


# --------------------------------------------------------------------------
# Validators (per step kind)
# --------------------------------------------------------------------------


def _validate(step: Step, user_input: str, collected: dict) -> Optional[Any]:
    text = (user_input or "").strip()
    if not text:
        return None

    if step.kind == STEP_ASK_TEXT:
        if len(text) < 2:
            return None
        return text[:500]  # cap to be sane

    if step.kind == STEP_ASK_NUMBER:
        # Strip "km", "kilometara" etc., grab digits
        digits = re.sub(r"[^\d]", "", text)
        if not digits:
            return None
        try:
            value = int(digits)
        except ValueError:
            return None
        if value < 0 or value > 9_999_999:
            return None
        return value

    if step.kind == STEP_ASK_PERIOD:
        # Parse Croatian period into {period_text, from_time, to_time}.
        # State machine raspakira ovaj dict u collected_params (line ~245).
        if len(text) < 4:
            return None
        parsed = _parse_period(text[:200])
        if parsed is None:
            return None
        return parsed

    if step.kind == STEP_ASK_CHOICE:
        choices = collected.get(step.choices_slot or "") or []
        if not choices:
            return None
        # Try numeric pick (1-based)
        if text.isdigit():
            idx = int(text) - 1
            if 0 <= idx < len(choices):
                return choices[idx]
        return None

    return None


_PERIOD_HOUR_RE = re.compile(
    r"(\d{1,2})(?::(\d{2}))?\s*[-–]\s*(\d{1,2})(?::(\d{2}))?"
)
_PERIOD_DATE_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})\.?\s*(.*)$")

# Croatian weekday names → ISO weekday (0=Monday).
_WEEKDAYS_HR = {
    "ponedjeljak": 0, "pon": 0,
    "utorak": 1, "uto": 1,
    "srijed": 2, "sri": 2,  # 'srijed' covers 'srijeda', 'srijedu'
    "četvrt": 3, "cetvrt": 3, "čet": 3, "cet": 3,
    "petak": 4, "pet": 4,
    "subot": 5, "sub": 5,
    "nedjelj": 6, "ned": 6,
}

# Parts of day → default time range (used only when user gives no explicit
# hours). Mapping calibrated to Croatian fleet driver workday patterns.
_PART_OF_DAY = {
    "ujutro":  (9, 12),    # morning
    "prijepodne": (9, 12),
    "popodne": (13, 17),   # afternoon
    "navečer": (18, 22),   # evening
    "naveer":  (18, 22),
    "uvece":   (18, 22),
}


def _resolve_weekday(today_date, name_lower: str, next_week: bool = False):
    """Return the date of the upcoming weekday matching `name_lower`.
    If next_week=True, skips this week's match and returns the one after.
    Returns None if no weekday keyword found."""
    today_weekday = today_date.weekday()  # 0=Mon
    for kw, target in _WEEKDAYS_HR.items():
        if kw in name_lower:
            delta = (target - today_weekday) % 7
            if delta == 0 and not next_week:
                # "u ponedjeljak" on a Monday → today; "iduci ponedjeljak" → +7
                delta = 7  # treat as "next" if ambiguous — driver expectation
            if next_week:
                if delta == 0:
                    delta = 7
                else:
                    delta += 7
            return today_date + timedelta(days=delta)
    return None


def _resolve_date(lower: str, today_date) -> tuple:
    """Resolve date component. Returns (date, remainder_text, source)
    where source is one of 'relative', 'weekday', 'dmy', 'default'."""
    if "prekosutra" in lower:
        return today_date + timedelta(days=2), lower.replace("prekosutra", "").strip(), "relative"
    if "sutra" in lower:
        return today_date + timedelta(days=1), lower.replace("sutra", "").strip(), "relative"
    if "danas" in lower:
        return today_date, lower.replace("danas", "").strip(), "relative"

    # Weekday names — "u petak", "iduci petak", "ovaj utorak", etc.
    next_week = bool(re.search(r"\b(?:iduć|iduci|sljedeć|sljedeci|drugi)\b", lower))
    weekday_date = _resolve_weekday(today_date, lower, next_week=next_week)
    if weekday_date is not None:
        # Strip weekday + modifiers from remainder
        rest = lower
        for kw in (
            "iduć", "iduci", "sljedeć", "sljedeci", "drugi",
            "ovaj", "u ", "tjedan",
        ):
            rest = rest.replace(kw, " ")
        for kw in _WEEKDAYS_HR:
            rest = rest.replace(kw, " ")
        return weekday_date, re.sub(r"\s+", " ", rest).strip(), "weekday"

    # Explicit DMY
    m = _PERIOD_DATE_RE.match(lower)
    if m:
        d, mo, y, tail = m.groups()
        try:
            d_iso = datetime(int(y), int(mo), int(d)).date()
        except ValueError:
            return None, lower, "invalid"
        return d_iso, (tail or "").strip(), "dmy"

    # No date hint — default to today
    return today_date, lower, "default"


def _resolve_hours(rest: str) -> Optional[tuple]:
    """Find HH-HH range in rest text. Returns (fh, fm, th, tm) or None.
    Falls back to part-of-day keywords (ujutro/popodne/navečer)."""
    # Explicit numeric range wins
    hm = _PERIOD_HOUR_RE.search(rest)
    if hm:
        fh, fm, th, tm = hm.groups()
        try:
            fh_i = int(fh)
            fm_i = int(fm) if fm else 0
            th_i = int(th)
            tm_i = int(tm) if tm else 0
        except ValueError:
            return None
        if not (0 <= fh_i <= 23 and 0 <= th_i <= 23 and 0 <= fm_i <= 59 and 0 <= tm_i <= 59):
            return None
        if (fh_i, fm_i) >= (th_i, tm_i):
            return None
        return fh_i, fm_i, th_i, tm_i

    # Part-of-day fallback
    for kw, (start, end) in _PART_OF_DAY.items():
        if kw in rest:
            return start, 0, end, 0
    return None


def _parse_period(text: str) -> Optional[dict]:
    """Parse Croatian period string into dict with ISO from/to times.

    Supported formats (Faza 4 Filip 2026-05-17 extended):
      Explicit ranges:
        "sutra 9-15"               → tomorrow 09:00-15:00
        "danas 9:30-17:00"         → today 09:30-17:00
        "prekosutra 8-16"          → +2 days 08:00-16:00
        "16.12.2025 9:00-17:00"    → explicit date + hours
        "9-15"                     → today 09:00-15:00
      Croatian weekdays (NEW):
        "u petak 9-15"             → upcoming Friday 09:00-15:00
        "iduci utorak 8-16"        → Tuesday of next week 08:00-16:00
        "u srijedu ujutro"         → Wednesday 09:00-12:00
      Parts of day (NEW):
        "sutra ujutro"             → tomorrow 09:00-12:00
        "danas popodne"            → today 13:00-17:00
        "navečer"                  → today 18:00-22:00

    Returns dict with `period_text`, `from_time`, `to_time` (all strings).
    `from_time` and `to_time` are ISO-8601 with seconds, no timezone (caller
    assumes local Europe/Zagreb). Returns None if parsing fails.
    """
    lower = text.lower().strip()
    today = datetime.now(_ZAGREB)
    date, rest, _src = _resolve_date(lower, today.date())
    if date is None:
        return None

    hours = _resolve_hours(rest)
    if hours is None:
        return None
    fh_i, fm_i, th_i, tm_i = hours

    from_dt = datetime.combine(date, dt_time(fh_i, fm_i))
    to_dt = datetime.combine(date, dt_time(th_i, tm_i))
    return {
        "period_text": text.strip()[:200],
        "from_time": from_dt.isoformat(timespec="seconds"),
        "to_time": to_dt.isoformat(timespec="seconds"),
    }


def _is_cancel(user_input: str) -> bool:
    n = (user_input or "").strip().lower()
    return n in ("odustani", "prekini", "cancel", "stop")


def _invalid_message(step: Step) -> str:
    if step.kind == STEP_ASK_NUMBER:
        return "Molim te unesi samo broj (npr. 145000)."
    if step.kind == STEP_ASK_PERIOD:
        return "Molim te navedi period (npr. 'sutra 9-15' ili '16.12.2025 9:00-17:00')."
    if step.kind == STEP_ASK_CHOICE:
        return "Molim te odaberi broj iz liste."
    if step.kind == STEP_ASK_TEXT:
        return "Molim te opiši situaciju u bar nekoliko riječi."
    return "Nisam razumio. Pokušaj ponovo."


def _render_prompt(template: str, collected: dict) -> str:
    """Substitute {slot} placeholders with values from collected_params.

    A dict-valued slot (e.g. booking's `chosen_vehicle`, a picked row) renders
    its human label, not the raw dict repr — otherwise the booking confirm
    would show "{'label': ..., 'Id': ...}".
    """
    out = template
    for k, v in collected.items():
        if isinstance(v, dict):
            disp = v.get("label") or v.get("Name") or v.get("name") or ""
            out = out.replace("{" + k + "}", str(disp))
        else:
            out = out.replace("{" + k + "}", str(v))
    return out


def _render_choices(prompt: str, choices: list) -> str:
    """Append numbered choices to a prompt."""
    if not choices:
        return prompt + "\n(Nema dostupnih opcija — postupak prekinut.)"
    lines = [prompt]
    for i, c in enumerate(choices, start=1):
        label = c.get("label") if isinstance(c, dict) else str(c)
        lines.append(f"{i}. {label}")
    lines.append("\nUnesi broj.")
    return "\n".join(lines)


def _resolve_template(template: dict, collected: dict) -> dict:
    """Replace `{slot}` placeholders inside a template dict's values."""
    out: dict = {}
    for k, v in template.items():
        if isinstance(v, str) and v.startswith("{") and v.endswith("}"):
            slot = v[1:-1]
            out[k] = collected.get(slot)
        else:
            out[k] = v
    return out


# --------------------------------------------------------------------------
# Flow definitions — DECLARATIVE, not hardcoded handlers
# --------------------------------------------------------------------------


def _booking_params(collected: dict) -> dict:
    """Build POST /vehiclemgt/VehicleCalendar payload from collected slots."""
    chosen = collected.get("chosen_vehicle") or {}
    return {
        "AssignedToId": collected.get("person_id"),
        "AssigneeType": 1,
        "EntryType": 0,
        "FromTime": collected.get("from_time"),
        "ToTime": collected.get("to_time"),
        "VehicleId": chosen.get("Id") if isinstance(chosen, dict) else None,
        "Description": None,
    }


def _mileage_params(collected: dict) -> dict:
    return {
        "VehicleId": collected.get("vehicle_id"),
        "Value": collected.get("mileage_value"),
        "Comment": "Unos preko WhatsApp bota",
    }


def _case_params(collected: dict) -> dict:
    return {
        "User": collected.get("person_id"),
        "Subject": collected.get("subject") or "Prijava preko bota",
        "Message": collected.get("description"),
    }


# Booking flow:
#   1. Ask period (from-to)
#   2. Lookup AvailableVehicles
#   3. Ask choice
#   4. Confirm
#   5. Execute POST VehicleCalendar
BOOKING_FLOW = Flow(
    name="booking",
    steps=(
        Step(
            kind=STEP_ASK_PERIOD,
            slot_name="period_text",
            prompt=(
                "Za koji period želiš rezervirati? "
                "(npr. 'sutra 9-15' ili '16.12.2025 9:00-17:00')"
            ),
        ),
        # ASK_PERIOD's validator (_parse_period) already fills from_time +
        # to_time (ISO) into collected_params, so this lookup's template
        # resolves them directly. The engine runs this read call and feeds
        # the rows back as the ASK_CHOICE options (see _continue_flow).
        Step(
            kind=STEP_EXEC_LOOKUP,
            slot_name="available_choices",
            lookup_tool_id="get_AvailableVehicles",
            lookup_params_template={
                "from": "{from_time}",
                "to": "{to_time}",
            },
        ),
        Step(
            kind=STEP_ASK_CHOICE,
            slot_name="chosen_vehicle",
            choices_slot="available_choices",
            prompt="Odaberi vozilo:",
        ),
        Step(
            kind=STEP_ASK_CONFIRM,
            slot_name="_confirmed",
            prompt=(
                "Rezervirat ću vozilo {chosen_vehicle} "
                "od {from_time} do {to_time}. Potvrđuješ? (Da/Ne)"
            ),
        ),
    ),
    final_tool_id="post_VehicleCalendar",
    final_params_builder=_booking_params,
)


MILEAGE_FLOW = Flow(
    name="mileage",
    steps=(
        Step(
            kind=STEP_ASK_NUMBER,
            slot_name="mileage_value",
            prompt="Koja je trenutna kilometraža? (samo broj, npr. 145000)",
        ),
        Step(
            kind=STEP_ASK_CONFIRM,
            slot_name="_confirmed",
            prompt=(
                "Upisat ću {mileage_value} km na vozilo {vehicle_name}. "
                "Potvrđuješ? (Da/Ne)"
            ),
        ),
    ),
    final_tool_id="post_AddMileage",
    final_params_builder=_mileage_params,
)


CASE_FLOW = Flow(
    name="case",
    steps=(
        Step(
            kind=STEP_ASK_TEXT,
            slot_name="description",
            prompt="Što se dogodilo? Opiši ukratko.",
        ),
        Step(
            kind=STEP_ASK_CONFIRM,
            slot_name="_confirmed",
            prompt=(
                "Prijavit ću slučaj: '{description}' "
                "za vozilo {vehicle_name}. Potvrđuješ? (Da/Ne)"
            ),
        ),
    ),
    final_tool_id="post_AddCase",
    final_params_builder=_case_params,
)


# Registry of flows by name — single source of truth.
FLOWS: dict[str, Flow] = {
    "booking": BOOKING_FLOW,
    "mileage": MILEAGE_FLOW,
    "case":    CASE_FLOW,
}


# --------------------------------------------------------------------------
# Persistence helper — Redis-backed, fault-tolerant.
# --------------------------------------------------------------------------


class FlowStateStore:
    """Tiny wrapper over Redis. Never raises on transient failures."""

    def __init__(self, redis_client, ttl_seconds: int = FLOW_TTL_SECONDS):
        self._redis = redis_client
        self._ttl = ttl_seconds

    @staticmethod
    def _key(phone: str) -> str:
        return _REDIS_PREFIX + phone.strip()

    async def load(self, phone: str) -> Optional[FlowState]:
        try:
            raw = await self._redis.get(self._key(phone))
        except Exception as e:  # noqa: BLE001
            logger.warning("flow state load failed: %s", e)
            return None
        if not raw:
            return None
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            return FlowState.from_json(raw)
        except (ValueError, TypeError, KeyError) as e:
            # PII fix: redact phone to last-4 in logs (GDPR — full phone is
            # personal data; pii_filter regex doesn't catch this since it's
            # passed as a positional arg, not in the message template).
            logger.warning(
                "flow state corrupt for %s: %s",
                (phone[-4:] if phone else ""), e,
            )
            return None

    async def save(self, phone: str, state: FlowState) -> None:
        try:
            await self._redis.setex(
                self._key(phone), self._ttl, state.to_json()
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("flow state save failed: %s", e)

    async def clear(self, phone: str) -> None:
        try:
            await self._redis.delete(self._key(phone))
        except Exception as e:  # noqa: BLE001
            logger.warning("flow state clear failed: %s", e)
