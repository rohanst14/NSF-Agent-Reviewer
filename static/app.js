const STORAGE_KEY = "nsf-reviewer-api-keys";

const state = {
    jobId: null,
    job: null,
    pollTimer: null,
    activeTab: "compliance",
};

const elements = {
    messageBox: document.getElementById("messageBox"),
    anthropicKey: document.getElementById("anthropicKey"),
    geminiKey: document.getElementById("geminiKey"),
    ollamaKey: document.getElementById("ollamaKey"),
    openaiKey: document.getElementById("openaiKey"),
    saveKeysToggle: document.getElementById("saveKeysToggle"),
    proposalText: document.getElementById("proposalText"),
    solicitationText: document.getElementById("solicitationText"),
    proposalFile: document.getElementById("proposalFile"),
    solicitationFile: document.getElementById("solicitationFile"),
    proposalFileName: document.getElementById("proposalFileName"),
    solicitationFileName: document.getElementById("solicitationFileName"),
    startReviewButton: document.getElementById("startReviewButton"),
    resetButton: document.getElementById("resetButton"),
    jobSummary: document.getElementById("jobSummary"),
    progressGrid: document.getElementById("progressGrid"),
    tabBar: document.getElementById("tabBar"),
    resultsContent: document.getElementById("resultsContent"),
};

const TAB_DEFINITIONS = [
    { id: "compliance", label: "Compliance" },
    { id: "reviewer_a", label: "Reviewer A" },
    { id: "reviewer_b", label: "Reviewer B" },
    { id: "reviewer_c", label: "Reviewer C" },
    { id: "discussion", label: "Discussion" },
    { id: "consensus", label: "Consensus" },
];


function init() {
    if (window.pdfjsLib) {
        pdfjsLib.GlobalWorkerOptions.workerSrc =
            "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js";
    }

    bindEvents();
    loadSavedKeys();
    renderProgress([]);
    renderTabs();
    renderResults();
}


function bindEvents() {
    document.querySelectorAll("[data-file-trigger]").forEach((button) => {
        button.addEventListener("click", () => {
            const targetId = button.getAttribute("data-file-trigger");
            document.getElementById(targetId).click();
        });
    });

    elements.proposalFile.addEventListener("change", (event) => handleFileInput(event, "proposal"));
    elements.solicitationFile.addEventListener("change", (event) => handleFileInput(event, "solicitation"));
    elements.startReviewButton.addEventListener("click", startReview);
    elements.resetButton.addEventListener("click", resetForm);
    elements.saveKeysToggle.addEventListener("change", persistKeysIfNeeded);
    elements.anthropicKey.addEventListener("input", persistKeysIfNeeded);
    elements.geminiKey.addEventListener("input", persistKeysIfNeeded);
    elements.ollamaKey.addEventListener("input", persistKeysIfNeeded);
    elements.openaiKey.addEventListener("input", persistKeysIfNeeded);
}


function loadSavedKeys() {
    try {
        const raw = localStorage.getItem(STORAGE_KEY);
        if (!raw) {
            return;
        }
        const parsed = JSON.parse(raw);
        elements.saveKeysToggle.checked = true;
        elements.anthropicKey.value = parsed.anthropic || "";
        elements.geminiKey.value = parsed.gemini || "";
        elements.ollamaKey.value = parsed.ollama || "";
        elements.openaiKey.value = parsed.openai || "";
    } catch (error) {
        console.error("Could not load stored keys", error);
    }
}


function persistKeysIfNeeded() {
    if (!elements.saveKeysToggle.checked) {
        localStorage.removeItem(STORAGE_KEY);
        return;
    }
    const payload = {
        anthropic: elements.anthropicKey.value.trim(),
        gemini: elements.geminiKey.value.trim(),
        ollama: elements.ollamaKey.value.trim(),
        openai: elements.openaiKey.value.trim(),
    };
    localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
}


async function handleFileInput(event, kind) {
    const file = event.target.files && event.target.files[0];
    if (!file) {
        return;
    }

    setMessage(`Loading ${file.name}...`, "info");
    try {
        const text = await readUploadedFile(file);
        if (kind === "proposal") {
            elements.proposalText.value = text;
            elements.proposalFileName.textContent = file.name;
        } else {
            elements.solicitationText.value = text;
            elements.solicitationFileName.textContent = file.name;
        }
        setMessage(`Loaded ${file.name}.`, "success");
    } catch (error) {
        setMessage(error.message, "error");
    }
}


async function readUploadedFile(file) {
    const name = file.name.toLowerCase();
    if (name.endsWith(".pdf")) {
        if (!window.pdfjsLib) {
            throw new Error("PDF.js did not load. Paste text manually or retry with network access.");
        }
        const buffer = await file.arrayBuffer();
        const pdf = await pdfjsLib.getDocument({ data: buffer }).promise;
        let text = "";
        for (let pageNumber = 1; pageNumber <= pdf.numPages; pageNumber += 1) {
            const page = await pdf.getPage(pageNumber);
            const content = await page.getTextContent();
            const pageText = content.items.map((item) => item.str).join(" ");
            text += `${pageText}\n`;
        }
        return text;
    }
    return file.text();
}


function collectAgentConfig(agentId) {
    return {
        provider: document.querySelector(`[data-agent-provider="${agentId}"]`).value,
        model: document.querySelector(`[data-agent-model="${agentId}"]`).value.trim(),
    };
}


function buildPayload() {
    return {
        api_keys: {
            anthropic: elements.anthropicKey.value.trim(),
            gemini: elements.geminiKey.value.trim(),
            ollama: elements.ollamaKey.value.trim(),
            openai: elements.openaiKey.value.trim(),
        },
        documents: {
            proposal: elements.proposalText.value.trim(),
            solicitation: elements.solicitationText.value.trim(),
        },
        agents: {
            compliance: collectAgentConfig("compliance"),
            reviewer_a: collectAgentConfig("reviewer_a"),
            reviewer_b: collectAgentConfig("reviewer_b"),
            reviewer_c: collectAgentConfig("reviewer_c"),
        },
    };
}


async function startReview() {
    const payload = buildPayload();
    if (payload.documents.proposal.length < 200) {
        setMessage("Proposal text is too short. Paste at least 200 characters.", "error");
        return;
    }
    if (payload.documents.solicitation.length < 100) {
        setMessage("Solicitation text is too short. Paste at least 100 characters.", "error");
        return;
    }

    persistKeysIfNeeded();
    setSubmitting(true);
    setMessage("Submitting review job...", "info");

    try {
        const response = await fetch("/api/review", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const data = await response.json();
        if (!response.ok) {
            throw new Error(data.error || "Review submission failed.");
        }
        state.jobId = data.job_id;
        state.job = data.job;
        state.activeTab = "compliance";
        renderJob(data.job);
        setMessage("Review started. Polling job status...", "success");
        startPolling();
    } catch (error) {
        setMessage(error.message, "error");
        setSubmitting(false);
    }
}


function setSubmitting(isSubmitting) {
    elements.startReviewButton.disabled = isSubmitting;
    elements.startReviewButton.textContent = isSubmitting ? "Running..." : "Start review";
}


function startPolling() {
    stopPolling();
    state.pollTimer = window.setInterval(fetchLatestJob, 2000);
    fetchLatestJob();
}


function stopPolling() {
    if (state.pollTimer) {
        window.clearInterval(state.pollTimer);
        state.pollTimer = null;
    }
}


async function fetchLatestJob() {
    if (!state.jobId) {
        return;
    }
    try {
        const response = await fetch(`/api/jobs/${state.jobId}`);
        const job = await response.json();
        if (!response.ok) {
            throw new Error(job.error || "Could not fetch job status.");
        }
        state.job = job;
        renderJob(job);

        if (job.status === "completed") {
            stopPolling();
            setSubmitting(false);
            setMessage("Review completed.", "success");
        } else if (job.status === "failed") {
            stopPolling();
            setSubmitting(false);
            setMessage("Review failed. Inspect the debug trace in the consensus tab.", "error");
        }
    } catch (error) {
        stopPolling();
        setSubmitting(false);
        setMessage(error.message, "error");
    }
}


function renderJob(job) {
    renderProgress(job.steps || []);
    renderSummary(job);
    renderResults();
}


function renderSummary(job) {
    const status = (job && job.status) || "idle";
    const label = status.charAt(0).toUpperCase() + status.slice(1);
    const submittedAt = job && job.submitted_at ? `Submitted ${job.submitted_at}` : "No active job.";
    elements.jobSummary.innerHTML = `
        <strong>${escapeHtml(label)}</strong>
        <div class="muted">${escapeHtml(submittedAt)}</div>
    `;
}


function renderProgress(steps) {
    if (!steps.length) {
        elements.progressGrid.innerHTML = "";
        return;
    }
    elements.progressGrid.innerHTML = steps
        .map(
            (step) => `
                <article class="progress-card">
                    <strong>${escapeHtml(step.label)}</strong>
                    <span class="status-badge ${statusClass(step.status)}">${escapeHtml(step.status)}</span>
                </article>
            `
        )
        .join("");
}


function renderTabs() {
    elements.tabBar.innerHTML = TAB_DEFINITIONS.map((tab) => {
        const active = tab.id === state.activeTab ? "active" : "";
        return `<button class="tab-button ${active}" type="button" data-tab="${tab.id}">${escapeHtml(tab.label)}</button>`;
    }).join("");

    elements.tabBar.querySelectorAll("[data-tab]").forEach((button) => {
        button.addEventListener("click", () => {
            state.activeTab = button.getAttribute("data-tab");
            renderTabs();
            renderResults();
        });
    });
}


function renderResults() {
    renderTabs();
    if (!state.job) {
        elements.resultsContent.innerHTML = `
            <div class="empty-results">
                Submit a job to populate compliance, reviewer reports, debate rounds, and the final consensus.
            </div>
        `;
        return;
    }

    const result = state.job.result || {};
    const content = renderActiveTab(result, state.job);
    elements.resultsContent.innerHTML = content;
}


function renderActiveTab(result, job) {
    switch (state.activeTab) {
        case "compliance":
            return renderComplianceTab(result.compliance);
        case "reviewer_a":
            return renderReviewerTab(result.reviewers && result.reviewers.reviewer_a, "Reviewer A");
        case "reviewer_b":
            return renderReviewerTab(result.reviewers && result.reviewers.reviewer_b, "Reviewer B");
        case "reviewer_c":
            return renderReviewerTab(result.reviewers && result.reviewers.reviewer_c, "Reviewer C");
        case "discussion":
            return renderDiscussionTab(result.discussion);
        case "consensus":
            return renderConsensusTab(result.consensus, job);
        default:
            return `<div class="empty-results">No data for this tab yet.</div>`;
    }
}


function renderComplianceTab(report) {
    if (!report) {
        return `<div class="empty-results">Compliance output will appear here once the first stage completes.</div>`;
    }

    const findings = (report.findings || [])
        .map(
            (item) => `
                <article class="round-message">
                    <div class="round-message-top">
                        <span class="round-message-name">${escapeHtml(item.area)}</span>
                        <span class="status-badge ${statusClass(item.severity === "blocking" ? "failed" : item.severity === "warning" ? "running" : "completed")}">${escapeHtml(item.severity)}</span>
                    </div>
                    <div class="round-message-text"><strong>Issue:</strong> ${escapeHtml(item.issue)}</div>
                    <div class="round-message-text"><strong>Recommendation:</strong> ${escapeHtml(item.recommendation)}</div>
                </article>
            `
        )
        .join("");

    return `
        <div class="report-shell">
            <section class="report-header">
                <div class="report-header-top">
                    <div>
                        <div class="report-kicker">Compliance result</div>
                        <h3 class="report-title">${escapeHtml(report.status)}</h3>
                    </div>
                    <span class="status-badge ${statusClass(report.status === "FAIL" ? "failed" : "completed")}">${escapeHtml(report.status)}</span>
                </div>
                <p class="report-body">${escapeHtml(report.executive_summary)}</p>
            </section>
            <section class="report-grid">
                <article class="report-section list-block">
                    <div class="subtle-label">Blocking issues</div>
                    ${renderList(report.blocking_issues)}
                </article>
                <article class="report-section list-block">
                    <div class="subtle-label">Recommendations</div>
                    ${renderList(report.recommendations)}
                </article>
            </section>
            <section class="round-block">
                <h4>Findings</h4>
                ${findings || `<div class="muted">No structured findings returned.</div>`}
            </section>
        </div>
    `;
}


function renderReviewerTab(report, fallbackTitle) {
    if (!report) {
        return `<div class="empty-results">${escapeHtml(fallbackTitle)} will appear here once that reviewer finishes.</div>`;
    }

    const scores = report.scores || {};
    return `
        <div class="report-shell">
            <section class="report-header">
                <div class="report-header-top">
                    <div>
                        <div class="report-kicker">${escapeHtml(report.reviewer_name)}</div>
                        <h3 class="report-title">${escapeHtml(report.vote)}</h3>
                    </div>
                    <span class="vote-badge ${voteClass(report.vote)}">${escapeHtml(report.vote)}</span>
                </div>
                <p class="report-body">${escapeHtml(report.executive_summary)}</p>
            </section>

            <section class="report-grid">
                <article class="metric-card">
                    <div class="metric-label">Intellectual Merit</div>
                    <div class="metric-value">${escapeHtml(formatScore(scores.intellectual_merit))}</div>
                </article>
                <article class="metric-card">
                    <div class="metric-label">Broader Impacts</div>
                    <div class="metric-value">${escapeHtml(formatScore(scores.broader_impacts))}</div>
                </article>
                <article class="metric-card">
                    <div class="metric-label">Technical</div>
                    <div class="metric-value">${escapeHtml(formatScore(scores.technical))}</div>
                </article>
                <article class="metric-card">
                    <div class="metric-label">Overall</div>
                    <div class="metric-value">${escapeHtml(formatScore(scores.overall))}</div>
                </article>
            </section>

            <section class="report-grid">
                <article class="report-section">
                    <div class="subtle-label">Intellectual Merit</div>
                    <p>${escapeHtml(report.intellectual_merit)}</p>
                </article>
                <article class="report-section">
                    <div class="subtle-label">Broader Impacts</div>
                    <p>${escapeHtml(report.broader_impacts)}</p>
                </article>
                <article class="report-section">
                    <div class="subtle-label">Technical Assessment</div>
                    <p>${escapeHtml(report.technical_assessment)}</p>
                </article>
                <article class="report-section list-block">
                    <div class="subtle-label">Questions</div>
                    ${renderList(report.questions)}
                </article>
                <article class="report-section list-block">
                    <div class="subtle-label">Strengths</div>
                    ${renderList(report.strengths)}
                </article>
                <article class="report-section list-block">
                    <div class="subtle-label">Weaknesses</div>
                    ${renderList(report.weaknesses)}
                </article>
            </section>
        </div>
    `;
}


function renderDiscussionTab(discussion) {
    if (!discussion) {
        return `<div class="empty-results">Discussion output will appear here after reviewer reports complete.</div>`;
    }

    const blocks = [
        { label: "Round 1: Opening positions", items: discussion.round_1 || [] },
        { label: "Round 2: Counter-points", items: discussion.round_2 || [] },
        { label: "Round 3: Final positions", items: discussion.final_positions || [] },
    ];

    return `
        <div class="discussion-shell">
            ${blocks
                .map(
                    (block) => `
                        <section class="round-block">
                            <h4>${escapeHtml(block.label)}</h4>
                            ${
                                block.items.length
                                    ? block.items.map(renderRoundMessage).join("")
                                    : `<div class="muted">No messages yet.</div>`
                            }
                        </section>
                    `
                )
                .join("")}
        </div>
    `;
}


function renderRoundMessage(message) {
    const showKeyReason = message.round === "final_positions" && message.key_reason;
    return `
        <article class="round-message">
            <div class="round-message-top">
                <span class="round-message-name">${escapeHtml(message.reviewer_name)}</span>
                <span class="vote-badge ${voteClass(message.vote)}">${escapeHtml(message.vote)}</span>
            </div>
            <div class="round-message-text">${escapeHtml(message.message)}</div>
            ${
                showKeyReason
                    ? `<div class="round-message-text"><strong>Key reason:</strong> ${escapeHtml(message.key_reason)}</div>`
                    : ""
            }
        </article>
    `;
}


function renderConsensusTab(consensus, job) {
    const failedTrace =
        job && job.status === "failed" && job.error
            ? `<section class="report-section"><div class="subtle-label">Debug trace</div><pre class="debug-trace">${escapeHtml(job.error)}</pre></section>`
            : "";

    if (!consensus) {
        return `
            <div class="consensus-shell">
                <div class="empty-results">Consensus output appears after final positions finish.</div>
                ${failedTrace}
            </div>
        `;
    }

    const votes = (consensus.vote_summary || [])
        .map(
            (item) => `
                <div class="pill">${escapeHtml(item.reviewer_name)}: ${escapeHtml(item.vote)}</div>
            `
        )
        .join("");

    const scoreSummary = consensus.score_summary || {};

    return `
        <div class="consensus-shell">
            <section class="consensus-header">
                <div class="consensus-top">
                    <div>
                        <div class="report-kicker">Panel outcome</div>
                        <h3>${escapeHtml(consensus.final_recommendation)}</h3>
                    </div>
                    <span class="vote-badge ${voteClass(consensus.final_recommendation)}">${escapeHtml(consensus.final_recommendation)}</span>
                </div>
                <p>${escapeHtml(consensus.summary)}</p>
                <div class="inline-pills">${votes || `<span class="muted">No reviewer votes recorded.</span>`}</div>
            </section>

            <section class="report-grid">
                <article class="metric-card">
                    <div class="metric-label">Average Merit</div>
                    <div class="metric-value">${escapeHtml(formatScore(scoreSummary.intellectual_merit))}</div>
                </article>
                <article class="metric-card">
                    <div class="metric-label">Average Impacts</div>
                    <div class="metric-value">${escapeHtml(formatScore(scoreSummary.broader_impacts))}</div>
                </article>
                <article class="metric-card">
                    <div class="metric-label">Average Technical</div>
                    <div class="metric-value">${escapeHtml(formatScore(scoreSummary.technical))}</div>
                </article>
                <article class="metric-card">
                    <div class="metric-label">Average Overall</div>
                    <div class="metric-value">${escapeHtml(formatScore(scoreSummary.overall))}</div>
                </article>
            </section>

            <section class="report-grid">
                <article class="report-section list-block">
                    <div class="subtle-label">Consensus strengths</div>
                    ${renderList(consensus.consensus_strengths)}
                </article>
                <article class="report-section list-block">
                    <div class="subtle-label">Consensus weaknesses</div>
                    ${renderList(consensus.consensus_weaknesses)}
                </article>
                <article class="report-section list-block">
                    <div class="subtle-label">Action items</div>
                    ${renderList(consensus.action_items)}
                </article>
                <article class="report-section list-block">
                    <div class="subtle-label">Dissenting views</div>
                    ${
                        (consensus.dissenting_views || []).length
                            ? `<ul>${consensus.dissenting_views
                                  .map(
                                      (item) =>
                                          `<li><strong>${escapeHtml(item.reviewer_name)}</strong> (${escapeHtml(item.vote)}): ${escapeHtml(
                                              item.reason
                                          )}</li>`
                                  )
                                  .join("")}</ul>`
                            : `<div class="muted">No dissent beyond the consensus outcome.</div>`
                    }
                </article>
            </section>
            ${failedTrace}
        </div>
    `;
}


function renderList(items) {
    if (!items || !items.length) {
        return `<div class="muted">No items recorded.</div>`;
    }
    return `<ul>${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`;
}


function statusClass(status) {
    const value = String(status || "pending").toLowerCase();
    if (value === "completed" || value === "done" || value === "pass") {
        return "status-completed";
    }
    if (value === "running" || value === "warning") {
        return "status-running";
    }
    if (value === "failed" || value === "fail" || value === "blocking") {
        return "status-failed";
    }
    if (value === "skipped") {
        return "status-skipped";
    }
    if (value === "blocked") {
        return "status-blocked";
    }
    return "status-pending";
}


function voteClass(vote) {
    const normalized = String(vote || "").toLowerCase().replace(/\s+/g, "-");
    switch (normalized) {
        case "recommend":
            return "vote-recommend";
        case "recommend-with-revisions":
            return "vote-recommend-with-revisions";
        case "reconsider":
            return "vote-reconsider";
        case "do-not-recommend":
            return "vote-do-not-recommend";
        default:
            return "status-pending";
    }
}


function formatScore(value) {
    if (value === null || value === undefined || value === "") {
        return "-";
    }
    const number = Number(value);
    if (!Number.isFinite(number)) {
        return "-";
    }
    return `${number.toFixed(1)}/5`;
}


function setMessage(text, kind = "info") {
    if (!text) {
        elements.messageBox.hidden = true;
        elements.messageBox.textContent = "";
        elements.messageBox.className = "message";
        return;
    }
    elements.messageBox.hidden = false;
    elements.messageBox.textContent = text;
    elements.messageBox.className = `message ${kind}`;
}


function resetForm() {
    stopPolling();
    state.jobId = null;
    state.job = null;
    state.activeTab = "compliance";
    elements.anthropicKey.value = "";
    elements.geminiKey.value = "";
    elements.ollamaKey.value = "";
    elements.openaiKey.value = "";
    elements.proposalText.value = "";
    elements.solicitationText.value = "";
    elements.proposalFile.value = "";
    elements.solicitationFile.value = "";
    elements.proposalFileName.textContent = "No file selected";
    elements.solicitationFileName.textContent = "No file selected";
    setSubmitting(false);
    setMessage("", "info");
    renderProgress([]);
    renderSummary({});
    renderResults();
}


function escapeHtml(value) {
    return String(value || "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
}


init();
