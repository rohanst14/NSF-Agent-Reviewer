from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
HOST = "127.0.0.1"
DEFAULT_PORT = 8000

MIN_PROPOSAL_CHARS = 200
MIN_SOLICITATION_CHARS = 100
MAX_PROPOSAL_CHARS = 50000
MAX_SOLICITATION_CHARS = 50000
REQUEST_TIMEOUT_SECONDS = 300

JOBS: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
EXECUTOR = ThreadPoolExecutor(max_workers=8)

STEP_ORDER = [
    ("compliance", "Compliance review"),
    ("reviewer_a", "Reviewer A report"),
    ("reviewer_b", "Reviewer B report"),
    ("reviewer_c", "Reviewer C report"),
    ("discussion_round_1", "Round 1 opening positions"),
    ("discussion_round_2", "Round 2 debate"),
    ("final_positions", "Round 3 final positions"),
    ("synthesis", "Final synthesis"),
]

REVIEWER_SPECS = [
    ("reviewer_a", "Reviewer A"),
    ("reviewer_b", "Reviewer B"),
    ("reviewer_c", "Reviewer C"),
]

DEFAULT_MODELS = {
    "anthropic": {
        "compliance": "claude-sonnet-4-20250514",
        "reviewer": "claude-sonnet-4-20250514",
    },
    "gemini": {
        "compliance": "gemini-2.5-flash-lite",
        "reviewer": "gemini-2.5-flash-lite",
    },
    "openai": {
        "compliance": "gpt-5.4-mini",
        "reviewer": "gpt-5.4-mini",
    },
    "ollama": {
        "compliance": "gemma3",
        "reviewer": "gemma3",
    },
}

AUTO_PROVIDER_ORDER = {
    "compliance": ["anthropic", "openai", "gemini", "ollama"],
    "reviewer_a": ["openai", "anthropic", "gemini", "ollama"],
    "reviewer_b": ["anthropic", "openai", "gemini", "ollama"],
    "reviewer_c": ["openai", "anthropic", "gemini", "ollama"],
}

VALID_VOTES = [
    "RECOMMEND",
    "RECOMMEND WITH REVISIONS",
    "RECONSIDER",
    "DO NOT RECOMMEND",
]

VOTE_SEVERITY = {
    "RECOMMEND": 4,
    "RECOMMEND WITH REVISIONS": 3,
    "RECONSIDER": 2,
    "DO NOT RECOMMEND": 1,
}

PROVIDER_MIN_INTERVAL_SECONDS = {
    "gemini": 4.5,
}

PROVIDER_RATE_LIMIT_LOCKS = {
    "gemini": threading.Lock(),
}

PROVIDER_NEXT_ALLOWED_AT = {
    "gemini": 0.0,
}


def utc_now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def json_dumps(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=True).encode("utf-8")


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def clamp_text(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = text[: int(limit * 0.7)].rstrip()
    tail = text[-int(limit * 0.25) :].lstrip()
    return f"{head}\n\n[... truncated for prompt budget ...]\n\n{tail}"


def extract_json_object(text: str) -> Dict[str, Any]:
    candidate = (text or "").strip()
    if not candidate:
        raise ValueError("Empty model response")

    fence_match = re.search(r"```(?:json)?\s*(\{.*\}|\[.*\])\s*```", candidate, re.DOTALL)
    if fence_match:
        candidate = fence_match.group(1).strip()

    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed
        raise ValueError("JSON response was not an object")
    except json.JSONDecodeError:
        pass

    start = None
    depth = 0
    in_string = False
    escape = False
    for index, char in enumerate(candidate):
        if start is None:
            if char == "{":
                start = index
                depth = 1
            continue

        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                snippet = candidate[start : index + 1]
                parsed = json.loads(snippet)
                if isinstance(parsed, dict):
                    return parsed
                raise ValueError("JSON response was not an object")

    raise ValueError("Could not extract JSON object from model response")


def slug_to_title(value: str) -> str:
    return value.replace("_", " ").title()


def sanitize_list(value: Any) -> List[str]:
    if isinstance(value, list):
        items = [normalize_whitespace(str(item)) for item in value if normalize_whitespace(str(item))]
        return items[:8]
    if isinstance(value, str):
        return [normalize_whitespace(value)] if normalize_whitespace(value) else []
    return []


def sanitize_vote(value: Any, default: str = "RECOMMEND WITH REVISIONS") -> str:
    normalized = normalize_whitespace(str(value or "")).upper()
    if normalized in VALID_VOTES:
        return normalized
    for vote in VALID_VOTES:
        if normalized.startswith(vote):
            return vote
    return default


def sanitize_score(value: Any, default: float = 3.0) -> float:
    try:
        score = round(float(value), 1)
    except (TypeError, ValueError):
        score = default
    return min(5.0, max(1.0, score))


def compute_overall_score(scores: Dict[str, float]) -> float:
    parts = [
        float(scores["intellectual_merit"]),
        float(scores["broader_impacts"]),
        float(scores["technical"]),
    ]
    return round(sum(parts) / len(parts), 1)


def normalize_optional_text(value: Any) -> str:
    return normalize_whitespace(str(value or ""))


def wait_for_provider_slot(provider: str) -> None:
    interval = PROVIDER_MIN_INTERVAL_SECONDS.get(provider, 0.0)
    if interval <= 0:
        return

    lock = PROVIDER_RATE_LIMIT_LOCKS.setdefault(provider, threading.Lock())
    with lock:
        now = time.monotonic()
        next_allowed_at = PROVIDER_NEXT_ALLOWED_AT.get(provider, 0.0)
        if next_allowed_at > now:
            time.sleep(next_allowed_at - now)
        PROVIDER_NEXT_ALLOWED_AT[provider] = time.monotonic() + interval


def parse_retry_delay(details: str) -> Optional[float]:
    match = re.search(r"retry in ([0-9]+(?:\.[0-9]+)?)s", details, re.IGNORECASE)
    if match:
        return float(match.group(1)) + 1.0
    return None


def request_json(url: str, headers: Dict[str, str], payload: Dict[str, Any], provider: str = "", retries: int = 0) -> Dict[str, Any]:
    wait_for_provider_slot(provider)
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8")
            return json.loads(body)
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="ignore")
        if error.code == 429 and retries < 4:
            delay = parse_retry_delay(details) or min(60.0, 5.0 * (2 ** retries))
            time.sleep(delay)
            return request_json(url, headers, payload, provider=provider, retries=retries + 1)
        if error.code in {500, 502, 503, 504} and retries < 4:
            delay = min(45.0, 4.0 * (2 ** retries))
            time.sleep(delay)
            return request_json(url, headers, payload, provider=provider, retries=retries + 1)
        raise RuntimeError(f"HTTP {error.code} from provider: {details[:700]}")
    except urllib.error.URLError as error:
        raise RuntimeError(f"Network error contacting provider: {error.reason}")


def call_anthropic(api_key: str, model: str, system_prompt: str, user_prompt: str) -> str:
    payload = {
        "model": model,
        "max_tokens": 2200,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    response = request_json(
        "https://api.anthropic.com/v1/messages",
        {
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        payload,
        provider="anthropic",
    )
    content = response.get("content") or []
    text_parts = [item.get("text", "") for item in content if item.get("type") == "text"]
    return "\n".join(part for part in text_parts if part).strip()


def call_openai(api_key: str, model: str, system_prompt: str, user_prompt: str) -> str:
    payload = {
        "model": model,
        "reasoning": {"effort": "medium"},
        "instructions": system_prompt,
        "input": user_prompt,
    }
    response = request_json(
        "https://api.openai.com/v1/responses",
        {
            "content-type": "application/json",
            "authorization": f"Bearer {api_key}",
        },
        payload,
        provider="openai",
    )
    output_text = response.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    output = response.get("output") or []
    text_chunks: List[str] = []
    for item in output:
        for chunk in item.get("content", []):
            if chunk.get("type") == "output_text" and chunk.get("text"):
                text_chunks.append(chunk["text"])
    if text_chunks:
        return "\n".join(text_chunks).strip()
    raise RuntimeError("OpenAI response did not include output text")


def call_gemini(api_key: str, model: str, system_prompt: str, user_prompt: str) -> str:
    payload = {
        "system_instruction": {
            "parts": [{"text": system_prompt}],
        },
        "contents": [
            {
                "role": "user",
                "parts": [{"text": user_prompt}],
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
        },
    }
    response = request_json(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        {
            "content-type": "application/json",
            "x-goog-api-key": api_key,
        },
        payload,
        provider="gemini",
    )
    candidates = response.get("candidates") or []
    text_chunks: List[str] = []
    for candidate in candidates:
        content = candidate.get("content") or {}
        for part in content.get("parts", []):
            if part.get("text"):
                text_chunks.append(part["text"])
    if text_chunks:
        return "\n".join(text_chunks).strip()
    raise RuntimeError("Gemini response did not include generated text")


def call_ollama(api_key: str, model: str, system_prompt: str, user_prompt: str) -> str:
    payload = {
        "model": model,
        "system": system_prompt,
        "prompt": user_prompt,
        "format": "json",
        "stream": False,
    }
    url = "http://localhost:11434/api/generate"
    headers = {"content-type": "application/json"}
    if api_key:
        url = "https://ollama.com/api/generate"
        headers["authorization"] = f"Bearer {api_key}"
    response = request_json(
        url,
        headers,
        payload,
        provider="ollama",
    )
    text = response.get("response")
    if isinstance(text, str) and text.strip():
        return text.strip()
    raise RuntimeError("Ollama response did not include generated text")


def select_provider(agent_id: str, preferred: str, keys: Dict[str, str]) -> str:
    preferred = (preferred or "auto").lower()
    if preferred in {"anthropic", "openai", "gemini", "ollama"}:
        if preferred != "ollama" and not keys.get(preferred):
            raise ValueError(f"{slug_to_title(agent_id)} is configured for {preferred}, but no key was provided.")
        return preferred

    for provider in AUTO_PROVIDER_ORDER.get(agent_id, ["openai", "anthropic"]):
        if provider == "ollama":
            return provider
        if keys.get(provider):
            return provider
    raise ValueError(f"{slug_to_title(agent_id)} could not auto-select a provider because no API keys were supplied.")


def resolve_agent_runtime(agent_id: str, config: Dict[str, Any], keys: Dict[str, str]) -> Dict[str, str]:
    provider = select_provider(agent_id, config.get("provider", "auto"), keys)
    model = normalize_whitespace(config.get("model", ""))
    if not model:
        model_type = "compliance" if agent_id == "compliance" else "reviewer"
        model = DEFAULT_MODELS[provider][model_type]
    return {"provider": provider, "model": model}


def run_model(agent_id: str, config: Dict[str, Any], keys: Dict[str, str], system_prompt: str, user_prompt: str) -> str:
    runtime = resolve_agent_runtime(agent_id, config, keys)
    provider = runtime["provider"]
    model = runtime["model"]
    if provider == "anthropic":
        return call_anthropic(keys["anthropic"], model, system_prompt, user_prompt)
    if provider == "openai":
        return call_openai(keys["openai"], model, system_prompt, user_prompt)
    if provider == "gemini":
        return call_gemini(keys["gemini"], model, system_prompt, user_prompt)
    return call_ollama(keys["ollama"], model, system_prompt, user_prompt)


def normalize_report_text(value: Any) -> str:
    text = normalize_whitespace(str(value or ""))
    return text if text else "No response provided."


def normalize_compliance_report(raw: Dict[str, Any], raw_text: str) -> Dict[str, Any]:
    status = normalize_whitespace(str(raw.get("status", "CONDITIONAL PASS"))).upper()
    if status not in {"PASS", "CONDITIONAL PASS", "FAIL"}:
        status = "CONDITIONAL PASS"
    findings = raw.get("findings")
    normalized_findings: List[Dict[str, str]] = []
    if isinstance(findings, list):
        for item in findings[:12]:
            if not isinstance(item, dict):
                continue
            normalized_findings.append(
                {
                    "area": normalize_report_text(item.get("area", "General")),
                    "severity": normalize_whitespace(str(item.get("severity", "warning"))).lower() or "warning",
                    "issue": normalize_report_text(item.get("issue", "")),
                    "recommendation": normalize_report_text(item.get("recommendation", "")),
                }
            )
    return {
        "status": status,
        "executive_summary": normalize_report_text(raw.get("executive_summary") or raw.get("summary")),
        "findings": normalized_findings,
        "blocking_issues": sanitize_list(raw.get("blocking_issues")),
        "non_blocking_issues": sanitize_list(raw.get("non_blocking_issues")),
        "recommendations": sanitize_list(raw.get("recommendations")),
        "raw_text": raw_text.strip(),
    }


def normalize_reviewer_report(reviewer_name: str, raw: Dict[str, Any], raw_text: str) -> Dict[str, Any]:
    scores = raw.get("scores") if isinstance(raw.get("scores"), dict) else {}
    normalized_scores = {
        "intellectual_merit": sanitize_score(scores.get("intellectual_merit")),
        "broader_impacts": sanitize_score(scores.get("broader_impacts")),
        "technical": sanitize_score(scores.get("technical")),
    }
    return {
        "reviewer_name": reviewer_name,
        "executive_summary": normalize_report_text(raw.get("executive_summary")),
        "intellectual_merit": normalize_report_text(raw.get("intellectual_merit")),
        "broader_impacts": normalize_report_text(raw.get("broader_impacts")),
        "technical_assessment": normalize_report_text(raw.get("technical_assessment")),
        "strengths": sanitize_list(raw.get("strengths")),
        "weaknesses": sanitize_list(raw.get("weaknesses")),
        "questions": sanitize_list(raw.get("questions")),
        "recommendations": sanitize_list(raw.get("recommendations")),
        "scores": {
            **normalized_scores,
            "overall": compute_overall_score(normalized_scores),
        },
        "vote": sanitize_vote(raw.get("vote")),
        "raw_text": raw_text.strip(),
    }


def normalize_discussion_message(reviewer_name: str, round_id: str, raw: Dict[str, Any], raw_text: str, vote_field: str) -> Dict[str, Any]:
    return {
        "reviewer_name": reviewer_name,
        "round": round_id,
        "message": normalize_report_text(raw.get("message") or raw.get("statement")),
        "vote": sanitize_vote(raw.get(vote_field) or raw.get("vote")),
        "confidence": normalize_whitespace(str(raw.get("confidence", ""))).lower(),
        "changed_position": bool(raw.get("changed_position", False)),
        "key_reason": normalize_optional_text(raw.get("key_reason", "")),
        "raw_text": raw_text.strip(),
    }


def build_compliance_prompt(proposal: str, solicitation: str) -> str:
    return f"""
Review the following NSF-style submission for compliance.

Return valid JSON only. Do not wrap the JSON in markdown fences.

Status rules:
- Use PASS only when there are no material compliance concerns.
- Use CONDITIONAL PASS when the proposal has fixable omissions, weak detail, or draft-quality compliance gaps but is still reviewable.
- Use FAIL only for true blocking issues such as clear eligibility problems, obviously missing core required sections, or administrative defects that should stop substantive review entirely.
- Do not mark FAIL for ordinary proposal weaknesses that belong in scientific review.

Required JSON schema:
{{
  "status": "PASS | CONDITIONAL PASS | FAIL",
  "executive_summary": "short paragraph",
  "findings": [
    {{
      "area": "requirement area",
      "severity": "blocking | warning | info",
      "issue": "what you found",
      "recommendation": "how to fix it"
    }}
  ],
  "blocking_issues": ["..."],
  "non_blocking_issues": ["..."],
  "recommendations": ["..."]
}}

Solicitation text:
\"\"\"
{solicitation}
\"\"\"

Proposal text:
\"\"\"
{proposal}
\"\"\"
""".strip()


def build_reviewer_prompt(reviewer_name: str, proposal: str, solicitation: str, compliance_report: Dict[str, Any]) -> str:
    return f"""
You are {reviewer_name}, one of three independent NSF-style reviewers.

Write your own complete review before panel discussion. Evaluate:
1. Intellectual Merit
2. Broader Impacts
3. Technical depth, rigor, and feasibility

Return valid JSON only. Do not include markdown fences.

Required JSON schema:
{{
  "executive_summary": "3-5 sentence summary",
  "intellectual_merit": "paragraphs",
  "broader_impacts": "paragraphs",
  "technical_assessment": "paragraphs",
  "strengths": ["3-6 concise bullets"],
  "weaknesses": ["3-6 concise bullets"],
  "questions": ["2-5 questions"],
  "recommendations": ["2-5 recommendations"],
  "scores": {{
    "intellectual_merit": 1,
    "broader_impacts": 1,
    "technical": 1
  }},
  "vote": "RECOMMEND | RECOMMEND WITH REVISIONS | RECONSIDER | DO NOT RECOMMEND"
}}

Scoring rules:
- Score each section on a 1-5 scale.
- 1 = very weak, 3 = adequate/mixed, 5 = outstanding.
- Do not use scores above 5.
- The system will compute the overall score from the three section scores.

Compliance context:
{json.dumps(compliance_report, ensure_ascii=True, indent=2)}

Solicitation text:
\"\"\"
{solicitation}
\"\"\"

Proposal text:
\"\"\"
{proposal}
\"\"\"
""".strip()


def build_round_one_prompt(reviewer_name: str, reviewer_report: Dict[str, Any]) -> str:
    return f"""
You are {reviewer_name} beginning panel discussion.

Summarize your independent review in a concise opening position.

Return valid JSON only:
{{
  "message": "2-4 paragraphs that summarize your position for the panel",
  "preliminary_vote": "RECOMMEND | RECOMMEND WITH REVISIONS | RECONSIDER | DO NOT RECOMMEND"
}}

Your report:
{json.dumps(reviewer_report, ensure_ascii=True, indent=2)}
""".strip()


def build_round_two_prompt(
    reviewer_name: str,
    reviewer_report: Dict[str, Any],
    peer_reports: List[Dict[str, Any]],
    round_one_messages: List[Dict[str, Any]],
) -> str:
    return f"""
You are {reviewer_name} in round 2 of a panel discussion.

Respond directly to the other reviewers. This is a real debate round, not a summary.

Debate requirements:
- Explicitly reference at least one point from each other reviewer by name.
- State where you agree and where you disagree.
- Challenge weak assumptions, unsupported claims, or overlooked risks.
- If another reviewer changed your view, say exactly how.
- Do not just restate your initial review.
- Ground your comments in the proposal, solicitation, and peer reports.

Return valid JSON only:
{{
  "message": "2-4 paragraphs responding to specific reviewer claims",
  "updated_vote": "RECOMMEND | RECOMMEND WITH REVISIONS | RECONSIDER | DO NOT RECOMMEND"
}}

Your report:
{json.dumps(reviewer_report, ensure_ascii=True, indent=2)}

Peer reports:
{json.dumps(peer_reports, ensure_ascii=True, indent=2)}

Round 1 discussion:
{json.dumps(round_one_messages, ensure_ascii=True, indent=2)}
""".strip()


def build_final_position_prompt(
    reviewer_name: str,
    reviewer_report: Dict[str, Any],
    discussion_messages: List[Dict[str, Any]],
) -> str:
    return f"""
You are {reviewer_name} concluding a panel discussion.

Return valid JSON only:
{{
  "message": "1-3 concise paragraphs stating your final position",
  "final_vote": "RECOMMEND | RECOMMEND WITH REVISIONS | RECONSIDER | DO NOT RECOMMEND",
  "confidence": "low | medium | high",
  "changed_position": true,
  "key_reason": "single concise sentence"
}}

Final-position requirements:
- State whether any reviewer changed your position.
- Mention at least one reviewer by name in explaining your final vote.
- Identify the single strongest unresolved disagreement or remaining concern.

Your report:
{json.dumps(reviewer_report, ensure_ascii=True, indent=2)}

Discussion so far:
{json.dumps(discussion_messages, ensure_ascii=True, indent=2)}
""".strip()


def build_consensus_summary(
    compliance_report: Dict[str, Any],
    reviewer_reports: Dict[str, Dict[str, Any]],
    final_positions: List[Dict[str, Any]],
) -> Dict[str, Any]:
    strengths = deduplicate_points(
        [point for report in reviewer_reports.values() for point in report.get("strengths", [])]
    )
    weaknesses = deduplicate_points(
        [point for report in reviewer_reports.values() for point in report.get("weaknesses", [])]
    )
    recommendations = deduplicate_points(
        [point for report in reviewer_reports.values() for point in report.get("recommendations", [])]
    )
    compliance_weaknesses = deduplicate_points(compliance_report.get("blocking_issues", []) + compliance_report.get("non_blocking_issues", []))
    compliance_actions = deduplicate_points(compliance_report.get("recommendations", []))

    votes = [position["vote"] for position in final_positions]
    final_vote = determine_final_vote(votes) if votes else "RECONSIDER"

    dissent = []
    for position in final_positions:
        if position["vote"] != final_vote:
            dissent.append(
                {
                    "reviewer_name": position["reviewer_name"],
                    "vote": position["vote"],
                    "reason": position.get("key_reason") or position.get("message"),
                }
            )

    score_summary = {}
    if reviewer_reports:
        score_summary = {
            "intellectual_merit": round(
                sum(report["scores"]["intellectual_merit"] for report in reviewer_reports.values()) / len(reviewer_reports), 1
            ),
            "broader_impacts": round(
                sum(report["scores"]["broader_impacts"] for report in reviewer_reports.values()) / len(reviewer_reports), 1
            ),
            "technical": round(
                sum(report["scores"]["technical"] for report in reviewer_reports.values()) / len(reviewer_reports), 1
            ),
            "overall": round(
                sum(report["scores"]["overall"] for report in reviewer_reports.values()) / len(reviewer_reports), 1
            ),
        }

    summary = compose_consensus_summary(final_vote, strengths, weaknesses, dissent, compliance_report["status"])

    return {
        "compliance_status": compliance_report["status"],
        "vote_summary": [
            {"reviewer_name": position["reviewer_name"], "vote": position["vote"], "confidence": position.get("confidence", "")}
            for position in final_positions
        ],
        "final_recommendation": final_vote,
        "score_summary": score_summary,
        "consensus_strengths": strengths[:6],
        "consensus_weaknesses": deduplicate_points(compliance_weaknesses + weaknesses)[:6],
        "action_items": deduplicate_points(compliance_actions + recommendations)[:6],
        "dissenting_views": dissent,
        "summary": summary,
    }


def compose_consensus_summary(
    final_vote: str,
    strengths: List[str],
    weaknesses: List[str],
    dissent: List[Dict[str, Any]],
    compliance_status: str,
) -> str:
    strength_text = "; ".join(strengths[:3]) if strengths else "no clear strengths were repeated across reviewers"
    weakness_text = "; ".join(weaknesses[:3]) if weaknesses else "no major shared weaknesses were identified"
    dissent_text = ""
    if dissent:
        dissent_text = f" Dissent remains from {', '.join(item['reviewer_name'] for item in dissent)}."
    if not strengths and not weaknesses and not dissent:
        return "The workflow completed with partial reviewer output, but not enough final debate positions were available to form a robust panel consensus."
    if compliance_status == "FAIL":
        return f"Panel outcome: {final_vote}. Reviewers completed substantive review, but compliance still failed and blocking administrative issues remain. Shared strengths include {strength_text}. Shared weaknesses include {weakness_text}.{dissent_text}"
    return f"Panel outcome: {final_vote}. Shared strengths include {strength_text}. Shared weaknesses include {weakness_text}.{dissent_text}"


def deduplicate_points(points: List[str]) -> List[str]:
    seen = set()
    output = []
    for point in points:
        normalized = normalize_whitespace(point).lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        output.append(point.strip())
    return output


def determine_final_vote(votes: List[str]) -> str:
    normalized_votes = [sanitize_vote(vote) for vote in votes]
    if not normalized_votes:
        return "RECONSIDER"
    counts = {vote: normalized_votes.count(vote) for vote in VALID_VOTES}

    if counts["DO NOT RECOMMEND"] >= 2:
        return "DO NOT RECOMMEND"
    if counts["RECONSIDER"] >= 2:
        return "RECONSIDER"
    if counts["RECOMMEND WITH REVISIONS"] >= 2:
        return "RECOMMEND WITH REVISIONS"
    if counts["RECOMMEND"] == 3:
        return "RECOMMEND"
    if counts["RECOMMEND"] == 2 and counts["RECOMMEND WITH REVISIONS"] == 1:
        return "RECOMMEND WITH REVISIONS"
    if counts["RECOMMEND"] == 1 and counts["RECOMMEND WITH REVISIONS"] == 1 and counts["RECONSIDER"] == 1:
        return "RECOMMEND WITH REVISIONS"
    if counts["RECOMMEND WITH REVISIONS"] == 1 and counts["RECONSIDER"] == 1 and counts["DO NOT RECOMMEND"] == 1:
        return "RECONSIDER"

    severe_sorted = sorted(normalized_votes, key=lambda vote: VOTE_SEVERITY[vote])
    return severe_sorted[1]


def initial_result() -> Dict[str, Any]:
    return {
        "compliance": None,
        "reviewers": {},
        "discussion": {
            "round_1": [],
            "round_2": [],
            "final_positions": [],
        },
        "consensus": None,
    }


def initial_steps() -> List[Dict[str, str]]:
    return [{"id": step_id, "label": label, "status": "pending"} for step_id, label in STEP_ORDER]


def create_job(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": uuid.uuid4().hex,
        "status": "queued",
        "submitted_at": utc_now(),
        "updated_at": utc_now(),
        "error": "",
        "steps": initial_steps(),
        "result": initial_result(),
        "request": {
            "proposal_length": len(payload["documents"]["proposal"]),
            "solicitation_length": len(payload["documents"]["solicitation"]),
            "agents": payload["agents"],
        },
    }


def update_job(job_id: str, mutator) -> None:
    with JOBS_LOCK:
        job = JOBS[job_id]
        mutator(job)
        job["updated_at"] = utc_now()


def set_job_status(job_id: str, status: str, error: str = "") -> None:
    def mutator(job: Dict[str, Any]) -> None:
        job["status"] = status
        job["error"] = error

    update_job(job_id, mutator)


def set_step_status(job_id: str, step_id: str, status: str) -> None:
    def mutator(job: Dict[str, Any]) -> None:
        for step in job["steps"]:
            if step["id"] == step_id:
                step["status"] = status
                break

    update_job(job_id, mutator)


def set_step_error(job_id: str, step_id: str, error: str) -> None:
    def mutator(job: Dict[str, Any]) -> None:
        for step in job["steps"]:
            if step["id"] == step_id:
                step["error"] = error[:800]
                break

    update_job(job_id, mutator)


def set_result_section(job_id: str, path: List[str], value: Any) -> None:
    def mutator(job: Dict[str, Any]) -> None:
        target = job["result"]
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    update_job(job_id, mutator)


def mark_following_steps_skipped(job_id: str, step_ids: List[str]) -> None:
    for step_id in step_ids:
        set_step_status(job_id, step_id, "skipped")


def validate_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Request body must be a JSON object.")

    documents = payload.get("documents") or {}
    proposal = (documents.get("proposal") or "").strip()
    solicitation = (documents.get("solicitation") or "").strip()
    if len(proposal) < MIN_PROPOSAL_CHARS:
        raise ValueError(f"Proposal text must be at least {MIN_PROPOSAL_CHARS} characters.")
    if len(solicitation) < MIN_SOLICITATION_CHARS:
        raise ValueError(f"Solicitation text must be at least {MIN_SOLICITATION_CHARS} characters.")

    keys = payload.get("api_keys") or {}
    normalized_keys = {
        "anthropic": (keys.get("anthropic") or "").strip(),
        "gemini": (keys.get("gemini") or "").strip(),
        "ollama": (keys.get("ollama") or "").strip(),
        "openai": (keys.get("openai") or "").strip(),
    }
    agent_payload = payload.get("agents") or {}
    normalized_agents = {}
    for agent_id in ["compliance", "reviewer_a", "reviewer_b", "reviewer_c"]:
        config = agent_payload.get(agent_id) or {}
        provider = (config.get("provider") or "auto").strip().lower()
        if provider not in {"auto", "anthropic", "openai", "gemini", "ollama"}:
            raise ValueError(f"Invalid provider for {agent_id}.")
        normalized_agents[agent_id] = {
            "provider": provider,
            "model": normalize_whitespace(config.get("model", "")),
        }
        resolve_agent_runtime(agent_id, normalized_agents[agent_id], normalized_keys)

    return {
        "api_keys": normalized_keys,
        "documents": {
            "proposal": clamp_text(proposal, MAX_PROPOSAL_CHARS),
            "solicitation": clamp_text(solicitation, MAX_SOLICITATION_CHARS),
        },
        "agents": normalized_agents,
    }


def run_compliance(payload: Dict[str, Any]) -> Dict[str, Any]:
    system_prompt = (
        "You are an NSF compliance reviewer. Evaluate formatting, required sections, "
        "eligibility, solicitation fit, and administrative completeness. Focus on objective compliance. "
        "Reserve FAIL for clear administrative blockers; use CONDITIONAL PASS for fixable draft-level issues."
    )
    raw_text = run_model(
        "compliance",
        payload["agents"]["compliance"],
        payload["api_keys"],
        system_prompt,
        build_compliance_prompt(payload["documents"]["proposal"], payload["documents"]["solicitation"]),
    )
    parsed = extract_json_object(raw_text)
    return normalize_compliance_report(parsed, raw_text)


def run_reviewer(
    reviewer_id: str,
    reviewer_name: str,
    payload: Dict[str, Any],
    compliance_report: Dict[str, Any],
) -> Dict[str, Any]:
    system_prompt = (
        "You are an expert NSF-style proposal reviewer. Write a rigorous, independent review "
        "covering intellectual merit, broader impacts, and technical feasibility."
    )
    raw_text = run_model(
        reviewer_id,
        payload["agents"][reviewer_id],
        payload["api_keys"],
        system_prompt,
        build_reviewer_prompt(
            reviewer_name,
            payload["documents"]["proposal"],
            payload["documents"]["solicitation"],
            compliance_report,
        ),
    )
    parsed = extract_json_object(raw_text)
    return normalize_reviewer_report(reviewer_name, parsed, raw_text)


def run_round_one_message(
    reviewer_id: str,
    reviewer_name: str,
    payload: Dict[str, Any],
    reviewer_report: Dict[str, Any],
) -> Dict[str, Any]:
    system_prompt = "You are participating in the first round of a proposal review panel."
    raw_text = run_model(
        reviewer_id,
        payload["agents"][reviewer_id],
        payload["api_keys"],
        system_prompt,
        build_round_one_prompt(reviewer_name, reviewer_report),
    )
    parsed = extract_json_object(raw_text)
    return normalize_discussion_message(reviewer_name, "round_1", parsed, raw_text, "preliminary_vote")


def run_round_two_message(
    reviewer_id: str,
    reviewer_name: str,
    payload: Dict[str, Any],
    reviewer_report: Dict[str, Any],
    peer_reports: List[Dict[str, Any]],
    round_one_messages: List[Dict[str, Any]],
) -> Dict[str, Any]:
    system_prompt = "You are participating in the second round of a proposal review panel debate."
    raw_text = run_model(
        reviewer_id,
        payload["agents"][reviewer_id],
        payload["api_keys"],
        system_prompt,
        build_round_two_prompt(reviewer_name, reviewer_report, peer_reports, round_one_messages),
    )
    parsed = extract_json_object(raw_text)
    return normalize_discussion_message(reviewer_name, "round_2", parsed, raw_text, "updated_vote")


def run_final_position_message(
    reviewer_id: str,
    reviewer_name: str,
    payload: Dict[str, Any],
    reviewer_report: Dict[str, Any],
    discussion_messages: List[Dict[str, Any]],
) -> Dict[str, Any]:
    system_prompt = "You are delivering your final panel position and vote."
    raw_text = run_model(
        reviewer_id,
        payload["agents"][reviewer_id],
        payload["api_keys"],
        system_prompt,
        build_final_position_prompt(reviewer_name, reviewer_report, discussion_messages),
    )
    parsed = extract_json_object(raw_text)
    return normalize_discussion_message(reviewer_name, "final_positions", parsed, raw_text, "final_vote")


def build_failed_reviewer_stub(reviewer_name: str, error: Exception) -> Dict[str, Any]:
    message = normalize_whitespace(str(error)) or "Reviewer execution failed."
    return {
        "reviewer_name": reviewer_name,
        "executive_summary": f"{reviewer_name} did not complete because of a provider or execution error: {message}",
        "intellectual_merit": "Review not completed.",
        "broader_impacts": "Review not completed.",
        "technical_assessment": "Review not completed.",
        "strengths": [],
        "weaknesses": [message],
        "questions": [],
        "recommendations": ["Retry the reviewer run or switch this reviewer to another provider/model."],
        "scores": {
            "intellectual_merit": 1.0,
            "broader_impacts": 1.0,
            "technical": 1.0,
            "overall": 1.0,
        },
        "vote": "RECONSIDER",
        "raw_text": message,
        "failed": True,
    }


def build_failed_discussion_stub(reviewer_name: str, round_id: str, error: Exception) -> Dict[str, Any]:
    message = normalize_whitespace(str(error)) or "Discussion message failed."
    return {
        "reviewer_name": reviewer_name,
        "round": round_id,
        "message": f"{reviewer_name} could not complete this discussion turn because of a provider or execution error: {message}",
        "vote": "RECONSIDER",
        "confidence": "low",
        "changed_position": False,
        "key_reason": "Discussion turn failed to complete.",
        "raw_text": message,
        "failed": True,
    }


def run_review_job(job_id: str, payload: Dict[str, Any]) -> None:
    try:
        set_job_status(job_id, "running")

        set_step_status(job_id, "compliance", "running")
        compliance_report = run_compliance(payload)
        set_result_section(job_id, ["compliance"], compliance_report)
        set_step_status(job_id, "compliance", "completed")

        reviewer_reports: Dict[str, Dict[str, Any]] = {}
        successful_reviewer_ids: List[str] = []
        reviewer_futures = {}
        for reviewer_id, reviewer_name in REVIEWER_SPECS:
            set_step_status(job_id, reviewer_id, "running")
            reviewer_futures[EXECUTOR.submit(run_reviewer, reviewer_id, reviewer_name, payload, compliance_report)] = (
                reviewer_id,
                reviewer_name,
            )

        for future in as_completed(reviewer_futures):
            reviewer_id, reviewer_name = reviewer_futures[future]
            try:
                report = future.result()
                reviewer_reports[reviewer_id] = report
                successful_reviewer_ids.append(reviewer_id)
                set_result_section(job_id, ["reviewers", reviewer_id], report)
                set_step_status(job_id, reviewer_id, "completed")
            except Exception as error:  # noqa: BLE001
                fallback_report = build_failed_reviewer_stub(reviewer_name, error)
                reviewer_reports[reviewer_id] = fallback_report
                set_result_section(job_id, ["reviewers", reviewer_id], fallback_report)
                set_step_status(job_id, reviewer_id, "completed")
                set_step_error(job_id, reviewer_id, str(error))

        if len(successful_reviewer_ids) < 2:
            mark_following_steps_skipped(job_id, ["discussion_round_1", "discussion_round_2", "final_positions"])
            set_step_status(job_id, "synthesis", "running")
            available_reports = {reviewer_id: reviewer_reports[reviewer_id] for reviewer_id in successful_reviewer_ids}
            consensus = build_consensus_summary(compliance_report, available_reports, [])
            consensus["summary"] = (
                "The workflow did not have enough completed reviewer reports to run debate. "
                "At least two reviewers must succeed for panel discussion."
            )
            set_result_section(job_id, ["consensus"], consensus)
            set_step_status(job_id, "synthesis", "completed")
            set_job_status(job_id, "completed")
            return

        set_step_status(job_id, "discussion_round_1", "running")
        round_one_messages = []
        round_one_futures = {}
        for reviewer_id, reviewer_name in REVIEWER_SPECS:
            if reviewer_id not in successful_reviewer_ids:
                continue
            round_one_futures[
                EXECUTOR.submit(run_round_one_message, reviewer_id, reviewer_name, payload, reviewer_reports[reviewer_id])
            ] = reviewer_id
        for future in as_completed(round_one_futures):
            reviewer_id = round_one_futures[future]
            reviewer_name = next(name for rid, name in REVIEWER_SPECS if rid == reviewer_id)
            try:
                round_one_messages.append(future.result())
            except Exception as error:  # noqa: BLE001
                round_one_messages.append(build_failed_discussion_stub(reviewer_name, "round_1", error))
        round_one_messages.sort(key=lambda item: item["reviewer_name"])
        set_result_section(job_id, ["discussion", "round_1"], round_one_messages)
        set_step_status(job_id, "discussion_round_1", "completed")

        set_step_status(job_id, "discussion_round_2", "running")
        round_two_messages = []
        round_two_futures = {}
        for reviewer_id, reviewer_name in REVIEWER_SPECS:
            if reviewer_id not in successful_reviewer_ids:
                continue
            peer_reports = [
                reviewer_reports[other_id]
                for other_id, _ in REVIEWER_SPECS
                if other_id != reviewer_id and other_id in successful_reviewer_ids
            ]
            round_two_futures[
                EXECUTOR.submit(
                    run_round_two_message,
                    reviewer_id,
                    reviewer_name,
                    payload,
                    reviewer_reports[reviewer_id],
                    peer_reports,
                    round_one_messages,
                )
            ] = reviewer_id
        for future in as_completed(round_two_futures):
            reviewer_id = round_two_futures[future]
            reviewer_name = next(name for rid, name in REVIEWER_SPECS if rid == reviewer_id)
            try:
                round_two_messages.append(future.result())
            except Exception as error:  # noqa: BLE001
                round_two_messages.append(build_failed_discussion_stub(reviewer_name, "round_2", error))
        round_two_messages.sort(key=lambda item: item["reviewer_name"])
        set_result_section(job_id, ["discussion", "round_2"], round_two_messages)
        set_step_status(job_id, "discussion_round_2", "completed")

        set_step_status(job_id, "final_positions", "running")
        final_positions = []
        discussion_history = round_one_messages + round_two_messages
        final_futures = {}
        for reviewer_id, reviewer_name in REVIEWER_SPECS:
            if reviewer_id not in successful_reviewer_ids:
                continue
            final_futures[
                EXECUTOR.submit(
                    run_final_position_message,
                    reviewer_id,
                    reviewer_name,
                    payload,
                    reviewer_reports[reviewer_id],
                    discussion_history,
                )
            ] = reviewer_id
        for future in as_completed(final_futures):
            reviewer_id = final_futures[future]
            reviewer_name = next(name for rid, name in REVIEWER_SPECS if rid == reviewer_id)
            try:
                final_positions.append(future.result())
            except Exception as error:  # noqa: BLE001
                final_positions.append(build_failed_discussion_stub(reviewer_name, "final_positions", error))
        final_positions.sort(key=lambda item: item["reviewer_name"])
        set_result_section(job_id, ["discussion", "final_positions"], final_positions)
        set_step_status(job_id, "final_positions", "completed")

        set_step_status(job_id, "synthesis", "running")
        available_reports = {reviewer_id: reviewer_reports[reviewer_id] for reviewer_id in successful_reviewer_ids}
        successful_final_positions = [position for position in final_positions if not position.get("failed")]
        consensus = build_consensus_summary(compliance_report, available_reports, successful_final_positions)
        set_result_section(job_id, ["consensus"], consensus)
        set_step_status(job_id, "synthesis", "completed")
        set_job_status(job_id, "completed")
    except Exception as error:  # noqa: BLE001
        trace = traceback.format_exc(limit=6)
        set_job_status(job_id, "failed", f"{error}\n\n{trace}")
        for step_id, _ in STEP_ORDER:
            update_job(
                job_id,
                lambda job, sid=step_id: _mark_running_as_failed(job, sid),
            )


def _mark_running_as_failed(job: Dict[str, Any], step_id: str) -> None:
    for step in job["steps"]:
        if step["id"] == step_id and step["status"] == "running":
            step["status"] = "failed"


class NSFReviewerHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self._send_json(HTTPStatus.OK, {"status": "ok", "time": utc_now()})
            return

        if parsed.path.startswith("/api/jobs/"):
            job_id = parsed.path.split("/")[-1]
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            if not job:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Job not found"})
                return
            self._send_json(HTTPStatus.OK, job)
            return

        if parsed.path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/api/review":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b"{}"
            payload = json.loads(body.decode("utf-8"))
            normalized = validate_payload(payload)
            job = create_job(normalized)
            with JOBS_LOCK:
                JOBS[job["id"]] = job
            worker = threading.Thread(target=run_review_job, args=(job["id"], normalized), daemon=True)
            worker.start()
            self._send_json(HTTPStatus.ACCEPTED, {"job_id": job["id"], "job": job})
        except json.JSONDecodeError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON body"})
        except ValueError as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except Exception as error:  # noqa: BLE001
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})

    def end_headers(self) -> None:
        self._send_cors_headers()
        super().end_headers()

    def _send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        response = json_dumps(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


def bind_server(host: str, preferred_port: int, attempts: int = 20) -> tuple[ThreadingHTTPServer, int]:
    last_error: Optional[Exception] = None
    for port in range(preferred_port, preferred_port + attempts):
        try:
            server = ThreadingHTTPServer((host, port), NSFReviewerHandler)
            return server, port
        except OSError as error:
            last_error = error
            if getattr(error, "errno", None) != 48:
                raise
    if last_error:
        raise last_error
    raise RuntimeError("Could not bind server to any candidate port.")


def run_server() -> None:
    if not STATIC_DIR.exists():
        raise SystemExit(f"Static directory not found: {STATIC_DIR}")

    preferred_port = int(os.environ.get("PORT", str(DEFAULT_PORT)))
    server, active_port = bind_server(HOST, preferred_port)
    if active_port != preferred_port:
        print(f"Port {preferred_port} was in use. Falling back to http://{HOST}:{active_port}")
    else:
        print(f"NSF reviewer app running at http://{HOST}:{active_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run_server()
