"""Tool Dispatcher — timeout, retries, invoked-tool tracking, and the required-tool guard (Task 12.1).

This is the component the design calls the **Tool Dispatcher** (design "Separation
of concerns" / the ``Tool Dispatcher`` node in the component diagram). The
orchestrator (the Bedrock Converse loop, Task 12.2) hands every ``toolUse`` request
Claude emits to this dispatcher instead of calling the Agent_Tools directly, so a
single place owns the operational guarantees around tool execution:

* **Timeout** — every Agent_Tool call is wrapped with a 10s timeout
  (:data:`ai_fit.config.TOOL_TIMEOUT_SECONDS`). A call that does not return within
  the window is abandoned and treated as a failure (Requirements 8.7, 9.6).
* **Retries** — a failed or timed-out call is retried up to
  :data:`ai_fit.config.TOOL_MAX_RETRIES` additional times (so at most ``1 +
  TOOL_MAX_RETRIES`` attempts). If every attempt fails, the dispatcher records the
  failure and surfaces it rather than raising (Requirements 8.7, 9.6).
* **Failure recording** — when all attempts for a call fail, the dispatcher records
  the failed tool and the data left unavailable to the Reasoning_Trace via
  :meth:`ai_fit.trace.builder.ReasoningTraceBuilder.record_tool_failure`
  (Requirements 7.6, 8.7). Successful calls are recorded with
  :meth:`record_tool_invocation` so the trace reflects true execution order.
* **Invoked-tool tracking** — the dispatcher remembers every tool name it attempted
  (success *or* failure counts as "invoked" for guard purposes: the model did call
  the tool). This set feeds the required-tool guard.
* **Required-tool guard** — before a size/occasion recommendation is presented, the
  orchestrator asks the dispatcher whether the mandatory tools for that request
  type were invoked; if not, the recommendation is rejected and never shown
  (Requirement 9.7, design "Orchestrator interface" required-tool guard). The guard
  logic is reused verbatim from the deterministic Guardrail Validator
  (:func:`ai_fit.reasoning.validator.required_tools_called`) so the dispatcher and
  the validator can never disagree about what "required" means.

The dispatcher is deliberately agnostic about *which* tools exist: it wraps any
callable the orchestrator registers (the 10 Agent_Tools in ``ai_fit/tools/*``),
each already bound to its ``DataStore`` / stores by the orchestrator. It performs no
Bedrock I/O itself — Bedrock throttling/non-throttling handling lives in the
Converse loop (Task 12.2), not here.

Timeout is implemented with a worker thread (:class:`concurrent.futures.ThreadPoolExecutor`)
rather than a signal alarm, so it works for arbitrary callables, off the main
thread, and on every platform. A timed-out worker thread cannot be force-killed in
Python; the dispatcher stops waiting on it and moves on, which is the correct
behavior for an I/O-bound tool call that has stopped responding.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

from ai_fit.config import TOOL_MAX_RETRIES, TOOL_TIMEOUT_SECONDS
from ai_fit.reasoning.validator import required_tools_called
from ai_fit.trace.builder import ReasoningTraceBuilder

# A tool is any callable the orchestrator binds (already closed over its store and
# arguments, or accepting ``**kwargs`` the dispatcher forwards).
ToolCallable = Callable[..., Any]


@dataclass(frozen=True)
class ToolDispatchResult:
    """The outcome of a single dispatched tool call.

    ``ok`` is ``True`` when some attempt returned a value within the timeout;
    ``value`` is that return value (``None`` on failure). ``attempts`` is how many
    attempts ran (1 + the retries actually used), ``timed_out`` flags that the final
    failure was a timeout rather than a raised error, and ``error`` carries the last
    exception message for diagnostics. A failed result is a value, not a raised
    exception: the dispatcher never lets a tool failure abort the Converse loop — the
    orchestrator decides how to continue with the partial results (Requirements 8.7,
    7.6).
    """

    tool_name: str
    ok: bool
    value: Any = None
    attempts: int = 0
    timed_out: bool = False
    error: Optional[str] = None


class ToolDispatcher:
    """Wraps Agent_Tool calls with timeout + retry, tracks invocations, and guards.

    Construct one dispatcher per agent run and pass it the run's
    :class:`ReasoningTraceBuilder` so successes and failures land on that run's
    trace. Call :meth:`dispatch` for every tool the model requests; query
    :meth:`invoked_tools` / :meth:`required_tools_satisfied` when deciding whether a
    recommendation may be presented.

    The timeout and retry budgets come from :mod:`ai_fit.config`
    (:data:`TOOL_TIMEOUT_SECONDS`, :data:`TOOL_MAX_RETRIES`) by default so there is a
    single source of truth; they can be overridden per dispatcher for tests.
    """

    def __init__(
        self,
        trace: ReasoningTraceBuilder,
        timeout_seconds: float = TOOL_TIMEOUT_SECONDS,
        max_retries: int = TOOL_MAX_RETRIES,
    ) -> None:
        self._trace = trace
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        # Every tool name the model asked us to run, in first-seen order. Both
        # successes and failures count: the guard asks "was the tool invoked",
        # which a failed-but-attempted call still satisfies (the gap is recorded in
        # the trace separately).
        self._invoked: List[str] = []

    # -- invoked-tool tracking --------------------------------------------------

    def invoked_tools(self) -> frozenset[str]:
        """Return the set of tool names dispatched so far (success or failure).

        This is the set the required-tool guard checks (Requirement 9.7). It feeds
        both :meth:`required_tools_satisfied` here and the Guardrail Validator's own
        guard, which receives the same set as ``Signals.invoked_tools``.
        """
        return frozenset(self._invoked)

    def _record_invocation(self, tool_name: str) -> None:
        """Remember that ``tool_name`` was attempted (idempotent per name)."""
        if tool_name not in self._invoked:
            self._invoked.append(tool_name)

    # -- the required-tool guard (Requirement 9.7) ------------------------------

    def required_tools_satisfied(
        self, request_type: str, customer_id_supplied: bool = False
    ) -> bool:
        """Return whether the mandatory tools for ``request_type`` were all invoked.

        Delegates to :func:`ai_fit.reasoning.validator.required_tools_called` so the
        dispatcher's guard and the validator's guard are the exact same predicate: a
        size recommendation needs ``get_sku_fit_signals``,
        ``get_product_catalog_entry``, and ``get_sku_reviews`` (plus
        ``get_customer_profile`` when a customer id was supplied); an occasion result
        needs ``get_product_catalog_entry``. If this returns ``False`` the
        orchestrator must reject the recommendation and not present it (design
        "Orchestrator interface", Requirement 9.7).
        """
        return required_tools_called(
            self.invoked_tools(), request_type, customer_id_supplied
        )

    # -- dispatch ---------------------------------------------------------------

    def dispatch(
        self,
        tool_name: str,
        tool: ToolCallable,
        *args: Any,
        unavailable_data: Optional[str] = None,
        **kwargs: Any,
    ) -> ToolDispatchResult:
        """Run ``tool(*args, **kwargs)`` with the timeout + retry policy.

        The call gets up to ``1 + max_retries`` attempts. Each attempt runs in a
        worker thread and is abandoned if it exceeds ``timeout_seconds``; a timeout
        or a raised exception consumes one attempt and triggers a retry while budget
        remains. On the first attempt that returns, the dispatcher records a
        successful tool invocation on the trace and returns an ``ok`` result. If
        every attempt fails, the dispatcher records a tool failure on the trace
        (naming ``unavailable_data`` so the gap is explicit — Requirements 7.6, 8.7)
        and returns a non-``ok`` result; it never raises, so the orchestrator can
        preserve partial results and continue.

        ``tool_name`` is tracked as invoked (for the required-tool guard) as soon as
        dispatch begins, regardless of the eventual outcome. ``unavailable_data``
        defaults to a readable phrase derived from ``tool_name`` when not supplied.
        """
        self._record_invocation(tool_name)
        missing = unavailable_data or self._default_unavailable(tool_name)

        last_error: Optional[str] = None
        timed_out = False
        attempts = 0
        total_attempts = 1 + max(0, self._max_retries)

        # A dedicated single-worker executor per dispatch so a timed-out, still-
        # running tool thread can be left behind without blocking subsequent calls.
        for attempt in range(1, total_attempts + 1):
            attempts = attempt
            executor = ThreadPoolExecutor(max_workers=1)
            future: Future = executor.submit(tool, *args, **kwargs)
            try:
                value = future.result(timeout=self._timeout_seconds)
            except FutureTimeout:
                timed_out = True
                last_error = f"timed out after {self._timeout_seconds}s"
                future.cancel()
                # Do not block on the stuck worker; let it finish and be GC'd.
                executor.shutdown(wait=False)
                continue
            except Exception as exc:  # noqa: BLE001 - any tool error is a failed attempt
                timed_out = False
                last_error = f"{type(exc).__name__}: {exc}"
                executor.shutdown(wait=False)
                continue
            else:
                executor.shutdown(wait=False)
                self._trace.record_tool_invocation(
                    tool_name=tool_name,
                    data_summary=value,
                    decision=f"{tool_name} returned on attempt {attempt}",
                )
                return ToolDispatchResult(
                    tool_name=tool_name,
                    ok=True,
                    value=value,
                    attempts=attempt,
                    timed_out=False,
                    error=None,
                )

        # Every attempt failed: record the gap in the trace and return a failed
        # result (Requirements 8.7, 7.6). The orchestrator keeps any partial results
        # and informs the customer the recommendation is incomplete.
        self._trace.record_tool_failure(
            tool_name=tool_name, unavailable_data=missing
        )
        return ToolDispatchResult(
            tool_name=tool_name,
            ok=False,
            value=None,
            attempts=attempts,
            timed_out=timed_out,
            error=last_error,
        )

    @staticmethod
    def _default_unavailable(tool_name: str) -> str:
        """Return a readable 'data unavailable' phrase for a tool name.

        Used in the trace's failed-tool record when the caller does not name the
        unavailable data explicitly, so the gap still reads sensibly for a
        Human_Reviewer (Requirement 7.6).
        """
        return f"data from {tool_name}"
