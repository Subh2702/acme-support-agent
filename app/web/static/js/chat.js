"use strict";

const STORAGE_KEY = "acme-session-id";
const $ = (id) => document.getElementById(id);
const messagesEl = $("messages");
const input = $("input");
const sendBtn = $("send");
let sessionId = safeGet(STORAGE_KEY);
let busy = false;

function safeGet(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}
function safeSet(key, value) {
  try { value ? localStorage.setItem(key, value) : localStorage.removeItem(key); } catch { /* private mode */ }
}

function escapeHtml(text) {
  return text.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/** Minimal, safe formatting: escape first, then **bold**, bullet lists and paragraphs. */
function formatReply(text) {
  const lines = escapeHtml(text).split(/\n/);
  let html = "";
  let list = null;
  for (const raw of lines) {
    const line = raw.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    const bullet = line.match(/^\s*(?:[-*•]|\d+\.)\s+(.*)$/);
    if (bullet) {
      if (!list) { html += "<ul>"; list = true; }
      html += `<li>${bullet[1]}</li>`;
      continue;
    }
    if (list) { html += "</ul>"; list = null; }
    if (line.trim()) html += `<p>${line}</p>`;
  }
  if (list) html += "</ul>";
  return html || "<p></p>";
}

function addMessage(role, text) {
  const wrap = document.createElement("div");
  wrap.className = `msg ${role}`;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.innerHTML = role === "user" ? `<p>${escapeHtml(text).replace(/\n/g, "<br>")}</p>` : formatReply(text);
  wrap.appendChild(bubble);
  messagesEl.appendChild(wrap);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function setTyping(on, label = "Thinking…") {
  $("typing").classList.toggle("show", on);
  $("typing-label").textContent = label;
  if (on) messagesEl.scrollTop = messagesEl.scrollHeight;
}

function setEscalated(escalated, ticketIds) {
  const banner = $("escalation-banner");
  banner.classList.toggle("show", !!escalated);
  if (escalated) {
    const ticket = ticketIds && ticketIds.length ? ` (ticket #${ticketIds[ticketIds.length - 1]})` : "";
    $("escalation-text").textContent = `A human specialist has been looped in${ticket}. You can keep chatting meanwhile.`;
  }
}

function setBusy(on) {
  busy = on;
  sendBtn.disabled = on;
  input.disabled = on;
  if (!on) input.focus();
}

function handleReply(data) {
  sessionId = data.session_id;
  safeSet(STORAGE_KEY, sessionId);
  addMessage("bot", data.reply);
  setEscalated(data.escalated, data.ticket_ids);
}

async function sendStreaming(message) {
  const res = await fetch("/api/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, message }),
  });
  if (!res.ok || !res.body) throw await errorFrom(res);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let gotReply = false;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const chunk = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      const event = (chunk.match(/^event: (.*)$/m) || [])[1];
      const dataLine = (chunk.match(/^data: (.*)$/m) || [])[1];
      if (!event || !dataLine) continue;
      const data = JSON.parse(dataLine);
      if (event === "session") { sessionId = data.session_id; safeSet(STORAGE_KEY, sessionId); }
      if (event === "progress") setTyping(true, `${data.label}…`);
      if (event === "reply") { handleReply(data); gotReply = true; }
      if (event === "error") throw new Error(data.detail);
    }
  }
  if (!gotReply) throw new Error("The connection closed early. Please try again.");
}

async function errorFrom(res) {
  let detail = `Request failed (${res.status})`;
  try {
    const body = await res.json();
    if (typeof body.detail === "string") detail = body.detail;
    else if (Array.isArray(body.detail)) detail = "Please check your message and try again.";
  } catch { /* not JSON */ }
  const err = new Error(detail);
  err.status = res.status;
  return err;
}

async function send(message) {
  if (busy || !message.trim()) return;
  addMessage("user", message);
  input.value = "";
  autoGrow();
  setBusy(true);
  setTyping(true);
  try {
    await sendStreaming(message);
  } catch (err) {
    addMessage("system", err.message || "Something went wrong. Please try again.");
  } finally {
    setTyping(false);
    setBusy(false);
  }
}

function autoGrow() {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 140)}px`;
}

async function restore() {
  if (!sessionId) return;
  try {
    const res = await fetch(`/api/chat/${encodeURIComponent(sessionId)}/history`);
    if (!res.ok) return;
    const data = await res.json();
    for (const m of data.messages) addMessage(m.role === "user" ? "user" : "bot", m.content);
    setEscalated(data.escalated, []);
  } catch { /* offline: start fresh */ }
}

async function loadDemoInfo() {
  try {
    const res = await fetch("/api/demo-info");
    const info = await res.json();
    $("model-badge").textContent = `model: ${info.model}`;
    const creds = $("creds");
    for (const c of info.credentials) {
      const div = document.createElement("div");
      div.className = "cred";
      div.innerHTML = `<b>${escapeHtml(c.name)}</b>
        <div class="mono">${escapeHtml(c.email)}</div>
        <div class="mono">${escapeHtml(c.order_number)} · PIN ${escapeHtml(c.pin_code)}</div>
        <button class="btn" type="button">Use these details</button>`;
      div.querySelector("button").addEventListener("click", () => {
        input.value = `My email is ${c.email}, order ${c.order_number}, PIN code ${c.pin_code}`;
        autoGrow();
        input.focus();
      });
      div.title = c.orders;
      creds.appendChild(div);
    }
    const prompts = $("prompts");
    for (const p of info.sample_prompts) {
      const b = document.createElement("button");
      b.className = "chip";
      b.type = "button";
      b.textContent = p;
      b.addEventListener("click", () => { input.value = p.replace(/\s*\(.*\)$/, ""); autoGrow(); input.focus(); });
      prompts.appendChild(b);
    }
  } catch { $("model-badge").textContent = "model: unavailable"; }
}

$("composer").addEventListener("submit", (e) => { e.preventDefault(); send(input.value); });
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(input.value); }
});
input.addEventListener("input", autoGrow);
$("new-chat").addEventListener("click", () => {
  sessionId = null;
  safeSet(STORAGE_KEY, null);
  messagesEl.querySelectorAll(".msg:not(:first-child)").forEach((n) => n.remove());
  setEscalated(false);
  input.focus();
});

restore();
loadDemoInfo();
