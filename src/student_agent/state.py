"""Typed A2A state and messages for the L3A pipeline.

Mapping to ``l3a-output-v2.schema.json`` (filled by Policy/Verifier in Pha 4):

- ``affected_entities``   <- ``EntityFindings.to_output()`` merged over specialist results
- ``evidence_refs``       <- refs of ``EvidenceItem`` that support the decision (subset of
                             ``CaseState.evidence_refs``; never generated or edited)
- ``data_conflicts``      <- ``DataConflict.to_output()``
- ``claim_assessments``   <- one per ``case["customer_request"]["claims"][*]["claim_id"]``
- missing evidence        <- ``MissingEvidence``; lowers confidence / ``insufficient_evidence``,
                             never replaced by guessed data
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from re import fullmatch
from typing import Any

EVIDENCE_REF_PATTERN = r"^ev_[A-Za-z0-9_-]{20,96}$"


class Actor(StrEnum):
    COORDINATOR = "coordinator"
    ORDER_ITEM = "order-item-agent"
    PAYMENT = "payment-agent"
    SHIPMENT = "shipment-agent"
    POLICY = "policy-agent"
    VERIFIER = "verifier-agent"


AGENT_ROLES = tuple(actor.value for actor in Actor)
SPECIALISTS = (Actor.ORDER_ITEM, Actor.PAYMENT, Actor.SHIPMENT)


class MissingReason(StrEnum):
    ID_NOT_IN_CASE = "id_not_in_case"
    PARAM_UNAVAILABLE = "param_unavailable"
    TOOL_NOT_DISCOVERED = "tool_not_discovered"
    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    TOOL_ERROR = "tool_error"
    TRANSIENT_EXHAUSTED = "transient_exhausted"
    INVALID_ENVELOPE = "invalid_envelope"
    DOMAIN_MISMATCH = "domain_mismatch"


# Keys read from gateway data to fill ``affected_entities``. Provisional until the real
# payload shapes are documented in docs/mcp-tools.md.
ENTITY_KEYS: Mapping[str, tuple[str, ...]] = {
    "order_ids": ("order_id",),
    "item_ids": ("order_item_id", "item_id"),
    "seller_ids": ("seller_id",),
    "payment_references": ("payment_reference", "payment_id"),
    "shipment_ids": ("shipment_id",),
}


def _scalar_id(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, int):
        return str(value)
    return None


def iter_id_fields(data: Any) -> Iterator[tuple[str, str]]:
    """Yield ``(key, value)`` for every scalar identifier field anywhere in ``data``."""
    if isinstance(data, Mapping):
        for key, value in data.items():
            if isinstance(value, Mapping | list):
                yield from iter_id_fields(value)
            elif isinstance(key, str) and (key.endswith("_id") or key == "payment_reference"):
                scalar = _scalar_id(value)
                if scalar is not None:
                    yield key, scalar
    elif isinstance(data, list):
        for value in data:
            yield from iter_id_fields(value)


class KnownIds:
    """Identifier values usable as tool arguments: from the case input or from gateway data."""

    def __init__(self) -> None:
        self._values: dict[str, list[str]] = {}

    def add(self, key: str, value: str) -> None:
        values = self._values.setdefault(key, [])
        if value not in values:
            values.append(value)

    def absorb(self, data: Any) -> None:
        for key, value in iter_id_fields(data):
            self.add(key, value)

    def merge(self, other: KnownIds) -> None:
        for key, values in other._values.items():
            for value in values:
                self.add(key, value)

    def get(self, key: str) -> tuple[str, ...]:
        return tuple(self._values.get(key, ()))

    def copy(self) -> KnownIds:
        clone = KnownIds()
        clone.merge(self)
        return clone

    @classmethod
    def from_case(cls, case: Mapping[str, Any]) -> KnownIds:
        """Seed only from explicit fields: ``claimed_<x>`` -> ``<x>`` and ``*_id`` keys."""
        known = cls()
        request = case.get("customer_request")
        if isinstance(request, Mapping):
            for key, value in request.items():
                scalar = _scalar_id(value)
                if scalar is None or not isinstance(key, str):
                    continue
                if key.startswith("claimed_"):
                    known.add(key.removeprefix("claimed_"), scalar)
                elif key.endswith("_id"):
                    known.add(key, scalar)
        return known


@dataclass(frozen=True)
class EvidenceItem:
    """One MCP evidence envelope, kept verbatim, plus who consumed it and how it was asked."""

    actor: str
    tool_name: str
    arguments: Mapping[str, str]
    evidence_ref: str
    result_hash: str
    domain: str
    data: Any
    warnings: tuple[str, ...] = ()

    @classmethod
    def from_envelope(
        cls,
        *,
        actor: str,
        tool_name: str,
        arguments: Mapping[str, str],
        envelope: Mapping[str, Any],
    ) -> EvidenceItem:
        return cls(
            actor=actor,
            tool_name=tool_name,
            arguments=dict(arguments),
            evidence_ref=envelope["evidence_ref"],
            result_hash=envelope["result_hash"],
            domain=envelope["domain"],
            data=envelope["data"],
            warnings=tuple(envelope.get("warnings", ())),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor": self.actor,
            "tool_name": self.tool_name,
            "arguments": dict(self.arguments),
            "evidence_ref": self.evidence_ref,
            "result_hash": self.result_hash,
            "domain": self.domain,
            "data": self.data,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class MissingEvidence:
    actor: str
    reason: MissingReason
    tool_name: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor": self.actor,
            "reason": self.reason.value,
            "tool_name": self.tool_name,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class DataConflict:
    """Same shape as ``dataConflict`` in the output schema, plus the observed values."""

    field: str
    sources: tuple[str, ...]
    selected_source: str | None
    resolution_code: str
    observed: Mapping[str, str] = field(default_factory=dict)

    def to_output(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "sources": list(self.sources),
            "selected_source": self.selected_source,
            "resolution_code": self.resolution_code,
        }


@dataclass(frozen=True)
class EntityFindings:
    order_ids: tuple[str, ...] = ()
    item_ids: tuple[str, ...] = ()
    seller_ids: tuple[str, ...] = ()
    payment_references: tuple[str, ...] = ()
    shipment_ids: tuple[str, ...] = ()

    @classmethod
    def from_evidence(cls, items: Iterable[EvidenceItem]) -> EntityFindings:
        found: dict[str, list[str]] = {name: [] for name in ENTITY_KEYS}
        for item in items:
            for key, value in iter_id_fields(item.data):
                for name, keys in ENTITY_KEYS.items():
                    if key in keys and value not in found[name]:
                        found[name].append(value)
        return cls(**{name: tuple(values) for name, values in found.items()})

    def merge(self, other: EntityFindings) -> EntityFindings:
        return EntityFindings(
            **{
                name: tuple(dict.fromkeys((*getattr(self, name), *getattr(other, name))))
                for name in ENTITY_KEYS
            }
        )

    def to_output(self) -> dict[str, list[str]]:
        return {name: list(getattr(self, name))[:20] for name in ENTITY_KEYS}


@dataclass(frozen=True)
class SpecialistResult:
    actor: str
    entities: EntityFindings
    evidence: tuple[EvidenceItem, ...]
    missing: tuple[MissingEvidence, ...] = ()
    conflicts: tuple[DataConflict, ...] = ()

    @property
    def evidence_refs(self) -> tuple[str, ...]:
        return tuple(item.evidence_ref for item in self.evidence)

    @property
    def complete(self) -> bool:
        return not self.missing

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor": self.actor,
            "entities": self.entities.to_output(),
            "evidence_refs": list(self.evidence_refs),
            "missing": [item.to_dict() for item in self.missing],
            "conflicts": [conflict.to_output() for conflict in self.conflicts],
        }


@dataclass(frozen=True)
class Handoff:
    """Observable A2A envelope passed between agents for one case."""

    case_id: str
    source: str
    target: str
    task: str
    entity_ids: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    attempt: int = 1
    payload: SpecialistResult | None = None

    def __post_init__(self) -> None:
        if self.source not in AGENT_ROLES or self.target not in AGENT_ROLES:
            raise ValueError("handoff source and target must be registered agent roles")
        if not self.case_id or not self.task or self.attempt < 1:
            raise ValueError("handoff requires case_id, task and positive attempt")
        if any(not fullmatch(EVIDENCE_REF_PATTERN, ref) for ref in self.evidence_refs):
            raise ValueError("handoff contains an invalid evidence_ref")


@dataclass
class EvidenceLedger:
    """Case-scoped evidence index; refs are accepted only from MCP responses."""

    case_id: str
    _evidence: dict[str, dict[str, Any]] = field(default_factory=dict)

    def record(self, evidence: dict[str, Any]) -> str:
        evidence_ref = evidence.get("evidence_ref")
        if not isinstance(evidence_ref, str) or not fullmatch(EVIDENCE_REF_PATTERN, evidence_ref):
            raise ValueError("MCP response has no valid evidence_ref")
        existing = self._evidence.get(evidence_ref)
        if existing is not None and existing != evidence:
            raise ValueError(f"evidence_ref reused with different content: {evidence_ref}")
        self._evidence[evidence_ref] = evidence
        return evidence_ref

    def contains(self, evidence_ref: str) -> bool:
        return evidence_ref in self._evidence

    def require(self, evidence_refs: list[str] | tuple[str, ...]) -> None:
        missing = [ref for ref in evidence_refs if not self.contains(ref)]
        if missing:
            raise ValueError(f"evidence refs are not in case ledger: {missing}")

    @property
    def refs(self) -> tuple[str, ...]:
        return tuple(self._evidence)


class CrossCaseGuard:
    """Process-wide check that one evidence_ref never serves two different cases."""

    def __init__(self) -> None:
        self._owner: dict[str, str] = {}

    def claim(self, case_id: str, evidence_ref: str) -> None:
        owner = self._owner.setdefault(evidence_ref, case_id)
        if owner != case_id:
            raise ValueError(f"evidence_ref {evidence_ref} already belongs to case {owner}")


@dataclass
class CaseState:
    case_id: str
    case: Mapping[str, Any]
    ledger: EvidenceLedger
    evidence: list[EvidenceItem] = field(default_factory=list)
    results: dict[str, SpecialistResult] = field(default_factory=dict)
    handoffs: list[Handoff] = field(default_factory=list)

    @classmethod
    def start(cls, case: Mapping[str, Any]) -> CaseState:
        case_id = case["case_id"]
        return cls(case_id=case_id, case=case, ledger=EvidenceLedger(case_id))

    def add_result(self, result: SpecialistResult) -> None:
        """Evidence must already be in the ledger (recorded when the MCP response arrived)."""
        self.ledger.require(result.evidence_refs)
        known = set(self.evidence_refs)
        for item in result.evidence:
            if item.evidence_ref not in known:
                self.evidence.append(item)
                known.add(item.evidence_ref)
        self.results[result.actor] = result

    @property
    def evidence_refs(self) -> tuple[str, ...]:
        return tuple(item.evidence_ref for item in self.evidence)

    @property
    def evidence_by_domain(self) -> dict[str, list[EvidenceItem]]:
        grouped: dict[str, list[EvidenceItem]] = {}
        for item in self.evidence:
            grouped.setdefault(item.domain, []).append(item)
        return grouped

    @property
    def missing(self) -> tuple[MissingEvidence, ...]:
        return tuple(entry for result in self.results.values() for entry in result.missing)

    @property
    def conflicts(self) -> tuple[DataConflict, ...]:
        return tuple(entry for result in self.results.values() for entry in result.conflicts)

    @property
    def entities(self) -> EntityFindings:
        merged = EntityFindings()
        for result in self.results.values():
            merged = merged.merge(result.entities)
        return merged

    def to_debug_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "evidence_refs": list(self.evidence_refs),
            "entities": self.entities.to_output(),
            "evidence_by_domain": {
                domain: [item.to_dict() for item in items]
                for domain, items in self.evidence_by_domain.items()
            },
            "specialists": {actor: result.to_dict() for actor, result in self.results.items()},
            "missing": [entry.to_dict() for entry in self.missing],
            "conflicts": [entry.to_output() for entry in self.conflicts],
            "handoffs": [
                {
                    "source": handoff.source,
                    "target": handoff.target,
                    "task": handoff.task,
                    "attempt": handoff.attempt,
                    "evidence_refs": list(handoff.evidence_refs),
                }
                for handoff in self.handoffs
            ],
        }
