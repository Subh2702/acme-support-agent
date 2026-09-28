"use strict";

const $ = (id) => document.getElementById(id);
const esc = (t) => String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, options) {
  const res = await fetch(path, { credentials: "same-origin", ...options });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

// ---------- tabs ----------
const loaders = { overview: loadStats, conversations: loadConversations, escalations: loadTickets, eval: loadEval };
document.querySelectorAll(".tab").forEach((btn) => btn.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b === btn));
  for (const name of Object.keys(loaders)) $(`tab-${name}`).hidden = name !== btn.dataset.tab;
  loaders[btn.dataset.tab]();
}));

// ---------- overview ----------
function stat(label, value) {
  return `<div class="card stat"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div></div>`;
}
function kvTable(obj) {
  const rows = Object.entries(obj || {}).sort((a, b) => b[1] - a[1]);
  return rows.length ? rows.map(([k, v]) => `<tr><td>${esc(k)}</td><td class="num">${v}</td></tr>`).join("")
    : `<tr><td class="empty">None yet</td></tr>`;
}
async function loadStats() {
  const s = await api("/api/admin/stats");
  $("model").textContent = s.model;
  $("stats").innerHTML = [
    stat("Conversations", s.conversations),
    stat("Escalation rate", `${(s.escalation_rate * 100).toFixed(1)}%`),
    stat("Open tickets", s.open_tickets),
    stat("LLM cost today", s.cost_today),
    stat("Daily budget", `${s.daily_budget} (${s.budget_used_pct}% used)`),
    stat("Total LLM cost", s.cost_total),
    stat("Cost / conversation", s.cost_per_conversation),
    stat("LLM calls", s.llm_calls),
    stat("Tokens in / out", `${s.input_tokens.toLocaleString("en-IN")} / ${s.output_tokens.toLocaleString("en-IN")}`),
    stat("Turn latency p50 / p95", `${(s.turn_latency_ms.p50 / 1000).toFixed(1)}s / ${(s.turn_latency_ms.p95 / 1000).toFixed(1)}s`),
    stat("LLM call p50 / p95", `${(s.llm_latency_ms.p50 / 1000).toFixed(1)}s / ${(s.llm_latency_ms.p95 / 1000).toFixed(1)}s`),
    stat("USD→INR rate", `₹${s.usd_inr_rate}`),
  ].join("");
  $("guardrails").innerHTML = kvTable(s.guardrail_hits);
  $("intents").innerHTML = kvTable(s.intents);
}
$("reset").addEventListener("click", async () => {
  if (!confirm("Wipe all conversations and reseed demo data?")) return;
  await api("/api/admin/reset-demo?confirm=RESET", { method: "POST" });
  loadStats();
});

// ---------- conversations ----------
async function loadConversations() {
  const data = await api("/api/admin/conversations?limit=100");
  const body = $("conv-list");
  body.innerHTML = data.items.length ? "" : `<tr><td colspan="4" class="empty">No conversations yet</td></tr>`;
  for (const c of data.items) {
    const tr = document.createElement("tr");
    tr.className = "clickable";
    const flags = [c.verified ? "verified" : "", c.escalated ? "escalated" : "", c.language === "hinglish" ? "hinglish" : ""]
      .filter(Boolean).map((f) => `<span class="pill">${f}</span>`).join(" ");
    tr.innerHTML = `<td>${esc(c.updated)}<div class="muted">${esc(c.preview)}</div></td>
      <td>${c.turns}</td><td class="num">${esc(c.cost)}</td><td>${flags}</td>`;
    tr.addEventListener("click", () => loadConversation(c.id));
    body.appendChild(tr);
  }
}

function eventLine(e) {
  const p = e.payload || {};
  const bits = {
    llm_call: () => `${p.stop_reason ?? p.error ?? ""} ${p.tool_calls?.length ? "→ " + p.tool_calls.join(", ") : ""}`,
    tool_call: () => (p.ok ? "ok" : `✗ ${p.error_code}`),
    policy_decision: () => `${p.decision} · ${p.reason}${p.order_number ? " · " + p.order_number : ""}`,
    guardrail: () => (p.labels || p.claims || p.leaks || p.redactions || []).join(", "),
    router: () => `${p.sentiment} · ${p.language}${p.signals?.length ? " · " + p.signals.join(", ") : ""}`,
    escalation: () => `ticket #${p.ticket_id ?? "?"}`,
    turn: () => p.status ?? "",
  }[e.kind];
  return bits ? bits() : "";
}

async function loadConversation(id) {
  const c = await api(`/api/admin/conversations/${encodeURIComponent(id)}`);
  const transcript = c.messages.map((m) =>
    `<div class="msg ${m.role === "user" ? "user" : "bot"}"><div class="bubble"><p>${esc(m.content)}</p></div></div>`).join("");
  const timeline = c.trace.map((e) => `
    <div class="event k-${esc(e.kind)}">
      <div><b>T${e.turn} · ${esc(e.kind)}</b> ${esc(e.name)} <span class="meta">${esc(eventLine(e))}</span></div>
      <div class="meta">${esc(e.at)} · ${e.latency_ms} ms${e.input_tokens ? ` · ${e.input_tokens}/${e.output_tokens} tok · ${esc(e.cost)}` : ""}</div>
      <details><summary class="meta">payload (PII-masked)</summary><pre>${esc(JSON.stringify(e.payload, null, 2))}</pre></details>
    </div>`).join("");
  const tickets = c.tickets.map(ticketCard).join("");
  $("conv-detail").innerHTML = `
    <h2>Conversation <span class="mono">${esc(c.id.slice(0, 12))}…</span> ${c.escalated ? '<span class="pill high">escalated</span>' : ""}</h2>
    ${tickets}
    <h3>Transcript</h3><div class="messages transcript scroll-340">${transcript}</div>
    <h3>Trace timeline</h3><div class="timeline">${timeline}</div>`;
}

// ---------- escalations ----------
function ticketCard(t) {
  const s = t.summary || {};
  const id = s.verified_identity || {};
  const list = (items, fmt) => items && items.length ? `<ul>${items.map(fmt).join("")}</ul>` : "<p class='small'>None</p>";
  return `<div class="card mb">
    <h2>#${t.id} <span class="pill ${esc(t.urgency)}">${esc(t.urgency)}</span> ${esc(t.reason_code)} ${t.order_number ? `· ${esc(t.order_number)}` : ""}</h2>
    <p class="small">${esc(t.created)} · status ${esc(t.status)}</p>
    <p><b>Issue:</b> ${esc(s.issue)}</p>
    <p><b>Identity:</b> ${id.verified ? `verified · ${esc(id.name)} · ${esc(id.email)} (customer #${id.customer_id})` : `not verified (${id.failed_verification_attempts ?? 0} failed attempts)`}</p>
    <b>Actions taken</b>${list(s.actions_taken, (a) => `<li>${esc(a.tool)} ${esc(a.order_number)} — ${esc(a.outcome)}: ${esc(a.detail)}</li>`)}
    <b>Actions denied</b>${list(s.actions_denied, (a) => `<li>${esc(a.tool)} ${esc(a.order_number)} — ${esc(a.reason_code)}: ${esc(a.why)}</li>`)}
    <p><b>Suggested next step:</b> ${esc(s.suggested_next_step)}</p>
    ${t.status === "open" ? `<button class="btn" data-resolve="${t.id}">Mark resolved</button>` : ""}
  </div>`;
}
async function loadTickets() {
  const data = await api("/api/admin/tickets?status=open");
  $("tickets").innerHTML = data.items.length ? data.items.map(ticketCard).join("") : `<div class="card empty">No open escalations 🎉</div>`;
  $("tickets").querySelectorAll("[data-resolve]").forEach((b) => b.addEventListener("click", async () => {
    await api(`/api/admin/tickets/${b.dataset.resolve}/resolve`, { method: "POST" });
    loadTickets();
  }));
}

// ---------- eval ----------
function renderMarkdown(md) {
  const lines = esc(md).split("\n");
  let html = "";
  let i = 0;
  const inline = (t) => t.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`(.+?)`/g, "<code>$1</code>");
  while (i < lines.length) {
    const line = lines[i];
    if (/^\|/.test(line)) {
      const rows = [];
      while (i < lines.length && /^\|/.test(lines[i])) rows.push(lines[i++]);
      const cells = (r) => r.split("|").slice(1, -1).map((c) => c.trim());
      const head = cells(rows[0]);
      const body = rows.slice(2).map(cells);
      html += `<table><thead><tr>${head.map((h) => `<th>${inline(h)}</th>`).join("")}</tr></thead><tbody>${body.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
      continue;
    }
    const h = line.match(/^(#{1,3}) (.*)$/);
    if (h) html += `<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`;
    else if (/^\s*[-*] /.test(line)) html += `<li>${inline(line.replace(/^\s*[-*] /, ""))}</li>`;
    else if (line.trim()) html += `<p>${inline(line)}</p>`;
    i++;
  }
  return html;
}
async function loadEval() {
  const data = await api("/api/admin/eval");
  $("eval").innerHTML = data.markdown ? renderMarkdown(data.markdown)
    : `<div class="empty">No eval results yet. Run <code>python -m eval.run_eval --config full</code>.</div>`;
}

loadStats().catch((e) => { $("stats").innerHTML = `<div class="card empty">${esc(e.message)}</div>`; });
