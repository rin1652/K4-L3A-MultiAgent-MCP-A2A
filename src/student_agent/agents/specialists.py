"""Specialist agents: each owns a fixed tool plan inside its permission scope."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from itertools import islice, product
from typing import Any, ClassVar

from ..state import (
    Actor,
    DataConflict,
    EntityFindings,
    EvidenceItem,
    KnownIds,
    MissingEvidence,
    MissingReason,
    SpecialistResult,
)
from ..trace import TraceWriter
from .toolbox import ScopedGateway, ToolCallFailure

# Parameters the case input itself is expected to provide.
CASE_PROVIDED_PARAMS = frozenset({"order_id"})


@dataclass(frozen=True)
class ToolStep:
    tool_name: str
    max_fanout: int = 5


def resolve_arguments(
    required: tuple[str, ...], known: KnownIds, max_fanout: int
) -> tuple[list[dict[str, str]], list[str]]:
    """Argument sets built only from known identifiers; returns ``(calls, absent_params)``."""
    if not required:
        return [{}], []
    values = [known.get(param) for param in required]
    absent = [param for param, options in zip(required, values, strict=True) if not options]
    if absent:
        return [], absent
    combos = islice(product(*values), max_fanout)
    return [dict(zip(required, combo, strict=True)) for combo in combos], []


class Specialist:
    actor: ClassVar[Actor]
    plan: ClassVar[tuple[ToolStep, ...]]

    def __init__(self, gateway: ScopedGateway, trace: TraceWriter, case: Mapping[str, Any]) -> None:
        if gateway.actor is not self.actor:
            raise ValueError("scoped gateway belongs to another actor")
        self._gateway = gateway
        self._trace = trace
        self.case = case
        self.case_id: str = case["case_id"]
        self.known = KnownIds()
        self.evidence: list[EvidenceItem] = []
        self.missing: list[MissingEvidence] = []
        self.deferred: list[ToolStep] = []

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(step.tool_name for step in self.steps)

    @property
    def steps(self) -> tuple[ToolStep, ...]:
        """The tools assigned to this agent for this case."""
        return self.plan

    async def run(self, seed: KnownIds) -> None:
        """Round 1: run the plan; steps lacking parameters are deferred, not guessed."""
        self.known.merge(seed)
        for step in self.steps:
            await self._run_step(step, final=False)

    def resolvable(self, shared: KnownIds) -> list[ToolStep]:
        pool = self.known.copy()
        pool.merge(shared)
        return [
            step
            for step in self.deferred
            if not resolve_arguments(self._required(step) or (), pool, step.max_fanout)[1]
        ]

    async def follow_up(self, shared: KnownIds) -> None:
        """Round 2 (at most once): retry deferred steps with ids found by other specialists."""
        self.known.merge(shared)
        steps, self.deferred = self.deferred, []
        for step in steps:
            await self._run_step(step, final=True)

    def finish(self) -> None:
        for step in self.deferred:
            required = self._required(step) or ()
            self._record_absent(step, [param for param in required if not self.known.get(param)])
        self.deferred = []

    def result(self) -> SpecialistResult:
        return SpecialistResult(
            actor=self.actor.value,
            entities=EntityFindings.from_evidence(self.evidence),
            evidence=tuple(self.evidence),
            missing=tuple(self.missing),
            conflicts=tuple(self.find_conflicts()),
        )

    def find_conflicts(self) -> list[DataConflict]:
        return []

    def _required(self, step: ToolStep) -> tuple[str, ...] | None:
        return self._gateway.required_params(step.tool_name)

    def _record_absent(self, step: ToolStep, absent: list[str]) -> None:
        for param in absent:
            reason = (
                MissingReason.ID_NOT_IN_CASE
                if param in CASE_PROVIDED_PARAMS
                else MissingReason.PARAM_UNAVAILABLE
            )
            self.missing.append(MissingEvidence(self.actor.value, reason, step.tool_name, param))

    async def _run_step(self, step: ToolStep, *, final: bool) -> None:
        required = self._required(step)
        if required is None:
            self.missing.append(
                MissingEvidence(self.actor.value, MissingReason.TOOL_NOT_DISCOVERED, step.tool_name)
            )
            return
        calls, absent = resolve_arguments(required, self.known, step.max_fanout)
        if absent:
            if final:
                self._record_absent(step, absent)
            else:
                self.deferred.append(step)
            return
        for arguments in calls:
            try:
                item = await self._gateway.call(step.tool_name, **arguments)
            except ToolCallFailure as failure:
                self.missing.append(
                    MissingEvidence(
                        self.actor.value, failure.reason, step.tool_name, failure.detail
                    )
                )
                continue
            if any(existing.evidence_ref == item.evidence_ref for existing in self.evidence):
                continue
            self.evidence.append(item)
            self.known.absorb(item.data)
            self._trace.emit(
                case_id=self.case_id,
                event_type="tool_result_consumed",
                actor=self.actor.value,
                tool_name=step.tool_name,
                evidence_refs=[item.evidence_ref],
                attributes={"domain": item.domain},
            )


class OrderItemAgent(Specialist):
    actor = Actor.ORDER_ITEM
    plan = (
        ToolStep("get_order"),
        ToolStep("get_order_items"),
        ToolStep("get_sellers"),
    )

    ITEM_TOPICS = frozenset(
        {
            "canceled_order_paid",
            "unavailable_order_paid",
            "late_delivery_seller",
            "late_delivery_logistics",
            "valid_split_payment",
            "payment_mismatch",
            "duplicate_charge",
        }
    )
    SELLER_TOPICS = frozenset({"unavailable_order_paid", "late_delivery_seller"})

    def _topics(self) -> set[str]:
        request = self.case.get("customer_request")
        claims = request.get("claims", ()) if isinstance(request, Mapping) else ()
        return {
            claim.get("topic")
            for claim in claims
            if isinstance(claim, Mapping) and isinstance(claim.get("topic"), str)
        }

    @property
    def steps(self) -> tuple[ToolStep, ...]:
        topics = self._topics()
        steps = [ToolStep("get_order")]
        if topics & self.ITEM_TOPICS:
            steps.append(ToolStep("get_order_items"))
        if topics & self.SELLER_TOPICS:
            steps.append(ToolStep("get_sellers"))
        return tuple(steps)

    def find_conflicts(self) -> list[DataConflict]:
        request = self.case.get("customer_request") or {}
        claimed = request.get("claimed_order_id") if isinstance(request, Mapping) else None
        conflicts = []
        for item in self.evidence:
            if item.tool_name != "get_order" or not isinstance(item.data, Mapping):
                continue
            actual = item.data.get("order_id")
            if claimed and isinstance(actual, str) and actual != claimed:
                conflicts.append(
                    DataConflict(
                        field="order_id",
                        sources=("customer_request.claimed_order_id", "get_order"),
                        selected_source=None,
                        resolution_code="order_identity_mismatch",
                        observed={
                            "customer_request.claimed_order_id": claimed,
                            "get_order": actual,
                        },
                    )
                )
        return conflicts


class PaymentAgent(Specialist):
    actor = Actor.PAYMENT
    plan = (
        ToolStep("get_order_payments"),
        ToolStep("get_payment_timeline"),
        ToolStep("get_refund_timeline"),
    )


class ShipmentAgent(Specialist):
    actor = Actor.SHIPMENT
    plan = (ToolStep("get_shipment_summary"),)


SPECIALIST_TYPES: tuple[type[Specialist], ...] = (OrderItemAgent, PaymentAgent, ShipmentAgent)
