"""Create an evidence-bound final analysis after the Agent loop has finished."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import Field

from api_doctor.agent.models import AgentRun
from api_doctor.generation.semantic_discovery import SemanticDiscovery
from api_doctor.generation.workflow_models import WorkflowDiscovery
from api_doctor.llm.gateway import ModelCallTelemetry, ModelGateway
from api_doctor.testspec.fusion import FinalTestPlan
from api_doctor.testspec.models import StrictModel
from api_doctor.workflows.validation import WorkflowValidationResult


class ReportFinding(StrictModel):
    title: str
    severity: Literal["low", "medium", "high"]
    summary: str
    evidence_refs: list[str] = Field(min_length=1)
    hypothesis_id: str | None = None
    recommendation: str


class ReportItem(StrictModel):
    title: str
    summary: str
    source_refs: list[str] = Field(min_length=1)


class ReportAnalysis(StrictModel):
    executive_summary: str
    confirmed_findings: list[ReportFinding] = Field(default_factory=list)
    possible_findings: list[ReportFinding] = Field(default_factory=list)
    ruled_out_hypotheses: list[ReportItem] = Field(default_factory=list)
    business_rule_ambiguities: list[ReportItem] = Field(default_factory=list)
    coverage_gaps: list[ReportItem] = Field(default_factory=list)
    next_steps: list[ReportItem] = Field(default_factory=list)


class ReportSynthesisResult(StrictModel):
    analysis: ReportAnalysis
    used_fallback: bool = False
    validation_errors: list[str] = Field(default_factory=list)
    synthesizer_error: str | None = None


@dataclass(frozen=True)
class ReportSynthesisOutcome:
    result: ReportSynthesisResult
    telemetry: ModelCallTelemetry


def synthesize_report(
    run: AgentRun,
    semantic_discovery: SemanticDiscovery,
    workflow_discovery: WorkflowDiscovery,
    workflow_validation: WorkflowValidationResult,
    test_plan: FinalTestPlan,
    gateway: ModelGateway,
) -> ReportSynthesisOutcome:
    prompt = build_report_prompt(
        run,
        semantic_discovery,
        workflow_discovery,
        workflow_validation,
        test_plan,
    )
    try:
        response = gateway.complete(
            prompt,
            response_model=ReportAnalysis,
            trace_label="report-synthesizer",
        )
    except Exception as error:
        return ReportSynthesisOutcome(
            result=ReportSynthesisResult(
                analysis=_fallback_analysis(run),
                used_fallback=True,
                synthesizer_error=f"Report synthesizer unavailable ({type(error).__name__})",
            ),
            telemetry=ModelCallTelemetry(calls=0, duration_ms=0),
        )

    errors = _validate_analysis(response.value, run, _source_refs(prompt))
    return ReportSynthesisOutcome(
        result=ReportSynthesisResult(
            analysis=_fallback_analysis(run) if errors else response.value,
            used_fallback=bool(errors),
            validation_errors=errors,
        ),
        telemetry=response.telemetry,
    )


def build_report_prompt(
    run: AgentRun,
    semantic_discovery: SemanticDiscovery,
    workflow_discovery: WorkflowDiscovery,
    workflow_validation: WorkflowValidationResult,
    test_plan: FinalTestPlan,
) -> dict[str, object]:
    state = run.state
    sources = _report_sources(
        run,
        semantic_discovery,
        workflow_discovery,
        workflow_validation,
        test_plan,
    )
    counts = {
        outcome: sum(item.outcome == outcome for item in state.observations)
        for outcome in ("passed", "failed", "error", "blocked")
    }
    return {
        "task": "Summarize the completed API test run using only supplied facts and source references.",
        "constraints": [
            "Do not invent requests, responses, defects, root causes, business rules, or coverage.",
            "Every finding must cite exact observation evidence_ref values from source_registry.",
            "A confirmed finding requires a confirmed hypothesis or a directly observed failed/error outcome.",
            "A possible finding may reference only an open hypothesis or a failed/error outcome.",
            "Every failed or error observation must appear in at least one confirmed or possible finding; a direct contract failure does not require a hypothesis_id.",
            "A ruled-out hypothesis must reference an exact rejected hypothesis_id.",
            "Ambiguities, coverage gaps, and next steps must cite exact source_ref values from source_registry.",
            "Distinguish observed behavior from inferred root cause and state uncertainty plainly.",
            "Do not describe blocked or unexecuted work as tested.",
        ],
        "run_summary": {
            "run_id": state.run_id,
            "termination_reason": state.termination_reason,
            "outcome_counts": counts,
            "remaining_test_budget": state.remaining_test_budget,
            "remaining_step_budget": state.remaining_step_budget,
            "completed_actions": len(state.completed_actions),
            "pending_actions": len(state.pending_actions),
        },
        "hypotheses": [item.model_dump(mode="json") for item in state.hypotheses],
        "source_registry": sources,
        "output_json_schema": ReportAnalysis.model_json_schema(),
    }


def _report_sources(
    run: AgentRun,
    semantic_discovery: SemanticDiscovery,
    workflow_discovery: WorkflowDiscovery,
    workflow_validation: WorkflowValidationResult,
    test_plan: FinalTestPlan,
) -> list[dict[str, object]]:
    sources: list[dict[str, object]] = [
        {
            "source_ref": item.evidence_ref,
            "kind": "observation",
            "action_id": item.action_id,
            "outcome": item.outcome,
            "summary": item.summary,
            "violations": item.violations,
        }
        for item in run.state.observations
    ]
    sources.extend(
        {
            "source_ref": f"hypothesis:{item.hypothesis_id}",
            "kind": "hypothesis",
            "status": item.status,
            "confidence": item.confidence,
            "summary": item.statement,
            "supporting_evidence": item.supporting_evidence,
            "contradicting_evidence": item.contradicting_evidence,
        }
        for item in run.state.hypotheses
    )
    sources.extend(
        {
            "source_ref": f"semantic-ambiguity:{index}",
            "kind": "semantic_ambiguity",
            "summary": text,
        }
        for index, text in enumerate(semantic_discovery.ambiguities, start=1)
    )
    sources.extend(
        {
            "source_ref": f"workflow-ambiguity:{index}",
            "kind": "workflow_ambiguity",
            "summary": text,
        }
        for index, text in enumerate(workflow_discovery.ambiguities, start=1)
    )
    for index, issue in enumerate(test_plan.rejected_candidates, start=1):
        sources.append(
            {
                "source_ref": f"test-plan:rejected/{index}",
                "kind": "rejected_candidate",
                "summary": "; ".join(issue.reasons),
                "rule_id": issue.candidate.rule_id,
            }
        )
    for index, issue in enumerate(test_plan.unsupported_candidates, start=1):
        sources.append(
            {
                "source_ref": f"test-plan:unsupported/{index}",
                "kind": "unsupported_candidate",
                "summary": "; ".join(issue.reasons),
                "rule_id": issue.candidate.rule_id,
            }
        )
    for review in workflow_validation.reviews:
        if review.verdict == "rejected":
            sources.append(
                {
                    "source_ref": f"workflow-validation:{review.workflow.workflow_id}",
                    "kind": "rejected_workflow",
                    "summary": "; ".join(review.errors),
                }
            )
    sources.extend(
        {
            "source_ref": f"pending:{item.kind}:{item.item_id}",
            "kind": "unexecuted_action",
            "summary": item.reason,
        }
        for item in run.state.pending_actions
    )
    sources.extend(
        [
            {
                "source_ref": "run:termination",
                "kind": "run_termination",
                "summary": run.state.termination_reason,
            },
            {
                "source_ref": "run:budget",
                "kind": "remaining_budget",
                "remaining_test_budget": run.state.remaining_test_budget,
                "remaining_step_budget": run.state.remaining_step_budget,
            },
        ]
    )
    return sources


def _source_refs(prompt: dict[str, object]) -> set[str]:
    return {
        item["source_ref"]
        for item in prompt["source_registry"]
        if isinstance(item, dict) and isinstance(item.get("source_ref"), str)
    }


def _validate_analysis(
    analysis: ReportAnalysis,
    run: AgentRun,
    known_sources: set[str],
) -> list[str]:
    errors: list[str] = []
    hypotheses = {item.hypothesis_id: item for item in run.state.hypotheses}
    observations = {item.evidence_ref: item for item in run.state.observations}

    for finding in analysis.confirmed_findings:
        _check_refs(finding.evidence_refs, known_sources, errors)
        hypothesis = hypotheses.get(finding.hypothesis_id) if finding.hypothesis_id else None
        if finding.hypothesis_id is not None and (
            hypothesis is None or hypothesis.status != "confirmed"
        ):
            errors.append(
                f"confirmed finding {finding.title!r} does not reference a confirmed hypothesis"
            )
        direct_failure = any(
            observations.get(ref) is not None
            and observations[ref].outcome in {"failed", "error"}
            for ref in finding.evidence_refs
        )
        if not direct_failure and not (hypothesis and hypothesis.status == "confirmed"):
            errors.append(f"confirmed finding {finding.title!r} lacks confirmed evidence")
    for finding in analysis.possible_findings:
        _check_refs(finding.evidence_refs, known_sources, errors)
        direct_failure = any(
            observations.get(ref) is not None
            and observations[ref].outcome in {"failed", "error"}
            for ref in finding.evidence_refs
        )
        if finding.hypothesis_id is not None:
            hypothesis = hypotheses.get(finding.hypothesis_id)
            if hypothesis is None or hypothesis.status != "open":
                errors.append(
                    f"possible finding {finding.title!r} does not reference an open hypothesis"
                )
        elif not direct_failure:
            errors.append(f"possible finding {finding.title!r} lacks failure evidence")
    for item in analysis.ruled_out_hypotheses:
        _check_refs(item.source_refs, known_sources, errors)
        hypothesis_id = next(
            (ref.removeprefix("hypothesis:") for ref in item.source_refs if ref.startswith("hypothesis:")),
            None,
        )
        if hypothesis_id is None or hypotheses.get(hypothesis_id) is None:
            errors.append(f"ruled-out item {item.title!r} lacks a known hypothesis reference")
        elif hypotheses[hypothesis_id].status != "rejected":
            errors.append(f"ruled-out item {item.title!r} references a non-rejected hypothesis")
    for items in (
        analysis.business_rule_ambiguities,
        analysis.coverage_gaps,
        analysis.next_steps,
    ):
        for item in items:
            _check_refs(item.source_refs, known_sources, errors)
    covered_failures = {
        ref
        for finding in [*analysis.confirmed_findings, *analysis.possible_findings]
        for ref in finding.evidence_refs
    }
    missing_failures = [
        item.evidence_ref
        for item in run.state.observations
        if item.outcome in {"failed", "error"}
        and item.evidence_ref not in covered_failures
    ]
    if missing_failures:
        errors.append(
            f"report omits failed/error observation(s) {missing_failures!r}"
        )
    return list(dict.fromkeys(errors))


def _check_refs(refs: list[str], known: set[str], errors: list[str]) -> None:
    unknown = [ref for ref in refs if ref not in known]
    if unknown:
        errors.append(f"report references unknown source(s) {unknown!r}")


def _fallback_analysis(run: AgentRun) -> ReportAnalysis:
    observations = run.state.observations
    counts = {
        outcome: sum(item.outcome == outcome for item in observations)
        for outcome in ("passed", "failed", "error", "blocked")
    }
    confirmed = [
        ReportFinding(
            title=f"Observed failure in {item.action_id}",
            severity="medium",
            summary=item.summary,
            evidence_refs=[item.evidence_ref],
            recommendation="Review the cited request and response evidence, fix the service, and rerun the case.",
        )
        for item in observations
        if item.outcome in {"failed", "error"}
    ]
    possible = [
        ReportFinding(
            title=item.statement,
            severity="medium",
            summary=f"Hypothesis remains open with {item.confidence} confidence.",
            evidence_refs=item.supporting_evidence or item.contradicting_evidence,
            hypothesis_id=item.hypothesis_id,
            recommendation="Run an evidence-distinguishing diagnostic before treating this as a root cause.",
        )
        for item in run.state.hypotheses
        if item.status == "open" and (item.supporting_evidence or item.contradicting_evidence)
    ]
    return ReportAnalysis(
        executive_summary=(
            f"Run finished with {counts['passed']} passed, {counts['failed']} failed, "
            f"{counts['error']} error, and {counts['blocked']} blocked observations. "
            f"Termination: {run.state.termination_reason}."
        ),
        confirmed_findings=confirmed,
        possible_findings=possible,
        coverage_gaps=[
            ReportItem(
                title="Unexecuted planned work",
                summary=f"{len(run.state.pending_actions)} action(s) remained pending.",
                source_refs=["run:termination"],
            )
        ]
        if run.state.pending_actions
        else [],
        next_steps=[
            ReportItem(
                title="Review remaining budget",
                summary="Use the saved budget and termination facts when deciding whether to rerun.",
                source_refs=["run:budget", "run:termination"],
            )
        ],
    )
