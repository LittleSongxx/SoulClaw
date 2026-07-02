<script setup lang="ts">
import {
  Activity,
  AlertTriangle,
  BookOpen,
  Brain,
  CheckCircle2,
  Database,
  FileText,
  Globe2,
  KeyRound,
  LogOut,
  Play,
  PlugZap,
  RefreshCw,
  Search,
  ServerCog,
  Settings,
  ShieldCheck,
  Timer,
  Workflow,
  Wrench
} from "@lucide/vue";
import { computed, onMounted, ref } from "vue";
import { createApiClient } from "./apiClient";
import type { ViewKey } from "./types";

const navItems: Array<{ key: ViewKey; label: string; icon: unknown }> = [
  { key: "dashboard", label: "Dashboard", icon: Activity },
  { key: "wiki", label: "Wiki", icon: BookOpen },
  { key: "context", label: "Context", icon: FileText },
  { key: "memory", label: "Memory", icon: Brain },
  { key: "skills", label: "Skills", icon: Wrench },
  { key: "tools", label: "Tools", icon: PlugZap },
  { key: "cron", label: "Cron", icon: Timer },
  { key: "mcp", label: "MCP", icon: PlugZap },
  { key: "gateways", label: "Gateways", icon: Globe2 },
  { key: "a2a", label: "A2A", icon: Workflow },
  { key: "heartbeat", label: "Heartbeat", icon: Activity },
  { key: "reliability", label: "Reliability", icon: ServerCog },
  { key: "sessions", label: "Sessions", icon: FileText },
  { key: "jobs", label: "Jobs", icon: Timer },
  { key: "proposals", label: "Proposals", icon: CheckCircle2 },
  { key: "approvals", label: "Approvals", icon: ShieldCheck },
  { key: "runs", label: "Runs", icon: Play },
  { key: "runGraph", label: "Run Graph", icon: Workflow },
  { key: "vector", label: "Vector", icon: Database },
  { key: "policy", label: "Policy", icon: ShieldCheck },
  { key: "audit", label: "Audit", icon: FileText },
  { key: "settings", label: "Settings", icon: Settings }
];

const token = ref(localStorage.getItem("soulclaw_token") || "");
const active = ref<ViewKey>("dashboard");
const username = ref("admin");
const password = ref("soulclaw-admin");
const loading = ref(false);
const error = ref("");
const searchQuery = ref("");
const memoryQuery = ref("");
const turnMessage = ref("");
const newMemory = ref("");
const jsonError = ref("");
const toolArgs = ref("{}");
const selectedTool = ref("");
const cronForm = ref({ name: "", cron_expr: "0 9 * * *", timezone: "Asia/Shanghai", instruction: "", enabled: true, metadata: "{}" });
const mcpForm = ref({ name: "", transport: "stdio", command: "", url: "", enabled: true, config: "{}" });
const gatewayForm = ref({ name: "", kind: "local", endpoint: "", enabled: true, config: "{}" });
const gatewayInbound = ref({ gateway_name: "", external_user_id: "console", text: "", channel_id: "", metadata: "{}" });
const gatewaySendForm = ref({ gateway_name: "", target_id: "", text: "", metadata: "{}" });
const a2aResumeDecision = ref("approve");
const a2aResumePayload = ref("{}");
const workspaceKind = ref("memory");
const workspaceContent = ref("");
const contextBlocks = ref<Array<Record<string, unknown>>>([]);
const contextProjections = ref<Array<Record<string, unknown>>>([]);
const contextBlockKey = ref("user");
const contextDraft = ref("");

const me = ref<Record<string, unknown> | null>(null);
const settingsData = ref<Record<string, unknown>>({});
const wikiHealth = ref<Record<string, unknown>>({});
const wikiPages = ref<Array<Record<string, unknown>>>([]);
const wikiSearchResults = ref<Array<Record<string, unknown>>>([]);
const wikiErrors = ref<Array<Record<string, unknown>>>([]);
const wikiOrientation = ref<Record<string, unknown> | null>(null);
const wikiLintResult = ref<Record<string, unknown> | null>(null);
const memories = ref<Array<Record<string, unknown>>>([]);
const skills = ref<Array<Record<string, unknown>>>([]);
const tools = ref<Array<Record<string, unknown>>>([]);
const cronJobs = ref<Array<Record<string, unknown>>>([]);
const mcpServers = ref<Array<Record<string, unknown>>>([]);
const gateways = ref<Array<Record<string, unknown>>>([]);
const a2aConnections = ref<Array<Record<string, unknown>>>([]);
const a2aTasks = ref<Array<Record<string, unknown>>>([]);
const sessions = ref<Array<Record<string, unknown>>>([]);
const jobs = ref<Array<Record<string, unknown>>>([]);
const sessionMessages = ref<Array<Record<string, unknown>>>([]);
const selectedSession = ref("");
const proposals = ref<Array<Record<string, unknown>>>([]);
const approvals = ref<Array<Record<string, unknown>>>([]);
const runs = ref<Array<Record<string, unknown>>>([]);
const agentRuns = ref<Array<Record<string, unknown>>>([]);
const selectedRunId = ref("");
const runGraph = ref<Record<string, unknown> | null>(null);
const vectorStatus = ref<Record<string, unknown>>({});
const vectorRebuildResult = ref<Record<string, unknown> | null>(null);
const policyStatus = ref<Record<string, unknown>>({});
const policyRules = ref<Array<Record<string, unknown>>>([]);
const events = ref<Array<Record<string, unknown>>>([]);
const audit = ref<Array<Record<string, unknown>>>([]);
const lastTurn = ref<Record<string, unknown> | null>(null);
const selectedWikiPage = ref<Record<string, unknown> | null>(null);
const selectedSkill = ref<Record<string, unknown> | null>(null);
const toolResult = ref<Record<string, unknown> | null>(null);
const jobResult = ref<Record<string, unknown> | null>(null);
const gatewayResult = ref<Record<string, unknown> | null>(null);
const a2aResult = ref<Record<string, unknown> | null>(null);
const selectedA2aTask = ref<Record<string, unknown> | null>(null);
const memoryConflicts = ref<Array<Record<string, unknown>>>([]);
const memoryProbes = ref<Array<Record<string, unknown>>>([]);
const workspaceFiles = ref<Array<Record<string, unknown>>>([]);
const heartbeatStatus = ref<Record<string, unknown> | null>(null);
const gatewayStatuses = ref<Array<Record<string, unknown>>>([]);
const reliabilityStatus = ref<Record<string, unknown>>({});
const reliabilityAlerts = ref<Array<Record<string, unknown>>>([]);
const reliabilityLimits = ref<Record<string, unknown>>({});
const recentTraces = ref<Array<Record<string, unknown>>>([]);

const loggedIn = computed(() => Boolean(token.value));
const pageTitle = computed(() => navItems.find((item) => item.key === active.value)?.label || "Dashboard");
const dreamLabel = computed(() => {
  if (!settingsData.value.dream_review_enabled) return "Off";
  return settingsData.value.dream_review_job_enabled ? "Scheduled" : "Not scheduled";
});
const displayedWikiItems = computed(() => {
  const items = wikiSearchResults.value.length ? wikiSearchResults.value : wikiPages.value;
  return items.map((item) => {
    const page = isRecord(item.page) ? item.page : item;
    return {
      page_key: String(page.page_key || ""),
      title: String(page.title || ""),
      summary: String(page.summary || ""),
      source: String(item.source || "")
    };
  });
});

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

async function api(path: string, init: RequestInit = {}) {
  return createApiClient(() => token.value, logout)(path, init);
}

async function login() {
  loading.value = true;
  error.value = "";
  try {
    const payload = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username: username.value, password: password.value })
    });
    token.value = payload.access_token;
    localStorage.setItem("soulclaw_token", token.value);
    await refreshAll();
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    loading.value = false;
  }
}

function logout() {
  token.value = "";
  localStorage.removeItem("soulclaw_token");
  me.value = null;
}

async function refreshAll() {
  if (!loggedIn.value) return;
  await Promise.all([loadMe(), loadSettings(), loadDashboard(), loadActive()]);
}

async function loadMe() {
  me.value = await api("/api/auth/me");
}

async function loadSettings() {
  settingsData.value = await api("/api/settings");
}

async function loadDashboard() {
  const [health, runtime] = await Promise.all([api("/api/wiki/health"), api("/api/events?limit=10")]);
  wikiHealth.value = health;
  events.value = runtime.items || [];
}

async function loadActive() {
  if (!loggedIn.value) return;
  if (active.value === "wiki") {
    const [pages, errorsPayload, health, orientPayload] = await Promise.all([
      api("/api/wiki/pages"),
      api("/api/wiki/errors"),
      api("/api/wiki/health"),
      api("/api/wiki/orient")
    ]);
    wikiPages.value = pages.items || [];
    wikiErrors.value = errorsPayload.items || [];
    wikiHealth.value = health;
    wikiOrientation.value = orientPayload;
  }
  if (active.value === "memory") memories.value = (await api("/api/memory")).items || [];
  if (active.value === "context") {
    const [blocks, projections] = await Promise.all([api("/api/context/blocks"), api("/api/context/projections")]);
    contextBlocks.value = blocks.items || [];
    contextProjections.value = projections.items || [];
  }
  if (active.value === "skills") skills.value = (await api("/api/skills")).items || [];
  if (active.value === "tools") tools.value = (await api("/api/tools")).items || [];
  if (active.value === "cron") cronJobs.value = (await api("/api/cron")).items || [];
  if (active.value === "mcp") mcpServers.value = (await api("/api/mcp")).items || [];
  if (active.value === "gateways") {
    const [items, statuses] = await Promise.all([api("/api/gateways"), api("/api/gateways/status")]);
    gateways.value = items.items || [];
    gatewayStatuses.value = statuses.items || [];
  }
  if (active.value === "a2a") {
    const [connections, tasks] = await Promise.all([api("/api/a2a/connections"), api("/api/a2a/tasks")]);
    a2aConnections.value = connections.items || [];
    a2aTasks.value = tasks.items || [];
    if (selectedA2aTask.value?.task_id) {
      await loadA2aTask(selectedA2aTask.value.task_id);
    }
  }
  if (active.value === "heartbeat") {
    const [status, files] = await Promise.all([api("/api/heartbeat/status"), api("/api/workspace/files")]);
    heartbeatStatus.value = status;
    workspaceFiles.value = files.items || [];
  }
  if (active.value === "reliability") {
    const [status, alerts, limits, traceEvents] = await Promise.all([
      api("/api/reliability"),
      api("/api/reliability/alerts"),
      api("/api/reliability/limits"),
      api("/api/events?limit=20")
    ]);
    reliabilityStatus.value = status;
    reliabilityAlerts.value = alerts.items || [];
    reliabilityLimits.value = limits;
    recentTraces.value = (traceEvents.items || []).filter((item: Record<string, unknown>) => item.trace_id);
  }
  if (active.value === "sessions") sessions.value = (await api("/api/sessions")).items || [];
  if (active.value === "jobs") jobs.value = (await api("/api/jobs")).items || [];
  if (active.value === "proposals") proposals.value = (await api("/api/evolution/proposals")).items || [];
  if (active.value === "approvals") approvals.value = (await api("/api/approvals")).items || [];
  if (active.value === "runs") runs.value = (await api("/api/runs")).items || [];
  if (active.value === "runGraph") {
    agentRuns.value = (await api("/api/agent-runs")).items || [];
    if (!selectedRunId.value && agentRuns.value[0]?.run_id) selectedRunId.value = String(agentRuns.value[0].run_id);
    if (selectedRunId.value) await loadRunGraph(selectedRunId.value);
  }
  if (active.value === "vector") vectorStatus.value = await api("/api/vector/status");
  if (active.value === "policy") {
    const [status, rules] = await Promise.all([api("/api/policy/status"), api("/api/policy/rules")]);
    policyStatus.value = status;
    policyRules.value = rules.items || [];
  }
  if (active.value === "audit") {
    audit.value = (await api("/api/audit")).items || [];
    events.value = (await api("/api/events")).items || [];
  }
  if (active.value === "settings") await loadSettings();
}

async function setView(key: ViewKey) {
  active.value = key;
  await loadActive();
}

async function compileWiki() {
  jobResult.value = await api("/api/wiki/compile", { method: "POST", body: JSON.stringify({ enqueue: true }) });
  await loadActive();
  await loadDashboard();
}

async function lintWiki() {
  jobResult.value = await api("/api/wiki/lint", { method: "POST", body: JSON.stringify({ enqueue: false }) });
  wikiLintResult.value = jobResult.value;
  await loadActive();
}

async function searchWiki() {
  const payload = await api("/api/wiki/search", {
    method: "POST",
    body: JSON.stringify({ query: searchQuery.value, limit: 10 })
  });
  wikiSearchResults.value = payload.items || [];
}

async function readWiki(pageKey: string) {
  selectedWikiPage.value = await api(`/api/wiki/read?page_key=${encodeURIComponent(pageKey)}`);
}

async function createMemory() {
  if (!newMemory.value.trim()) return;
  jobResult.value = await api("/api/memory", {
    method: "POST",
    body: JSON.stringify({
      kind: "agent_note",
      content: newMemory.value,
      source: "admin",
      importance: 0.55,
      confidence: 0.65,
      stability: 0.55
    })
  });
  newMemory.value = "";
  await loadActive();
}

async function proposeContextBlock() {
  if (!contextDraft.value.trim()) return;
  jobResult.value = await api(`/api/context/blocks/${encodeURIComponent(contextBlockKey.value)}/proposals`, {
    method: "POST",
    body: JSON.stringify({
      content: contextDraft.value,
      action: "update",
      risk_level: "medium",
      evidence: { source: "console.context" }
    })
  });
  contextDraft.value = "";
  await loadActive();
}

function editContextBlock(item: Record<string, unknown>) {
  contextBlockKey.value = String(item.block_key || "user");
  contextDraft.value = String(item.content || "");
}

async function loadWorkspaceFile(kind = workspaceKind.value) {
  const item = await api(`/api/workspace/files/${encodeURIComponent(kind)}`);
  workspaceKind.value = String(item.kind || kind);
  workspaceContent.value = String(item.content || "");
}

async function importWorkspaceDraft() {
  jobResult.value = await api(`/api/workspace/files/${encodeURIComponent(workspaceKind.value)}/import-draft`, {
    method: "POST",
    body: JSON.stringify({ content: workspaceContent.value })
  });
  await loadActive();
}

async function runHeartbeat(enqueue = true) {
  jobResult.value = await api(`/api/heartbeat/run?enqueue=${enqueue ? "true" : "false"}`, { method: "POST" });
  await loadActive();
}

async function searchMemory() {
  const payload = await api("/api/memory/search", {
    method: "POST",
    body: JSON.stringify({ query: memoryQuery.value, limit: 25 })
  });
  memories.value = (payload.items || []).map((item: Record<string, unknown>) => item.memory).filter(Boolean) as Array<Record<string, unknown>>;
}

async function importMemoryDraft() {
  jobResult.value = await api("/api/memory/import-draft", {
    method: "POST",
    body: JSON.stringify({ kind: workspaceKind.value, content: workspaceContent.value })
  });
  await loadActive();
}

async function verifyMemory(id: unknown) {
  jobResult.value = await api(`/api/memory/${id}/verify`, { method: "POST" });
  await loadActive();
}

async function archiveMemory(id: unknown) {
  jobResult.value = await api(`/api/memory/${id}/archive`, { method: "POST" });
  await loadActive();
}

async function loadMemoryDiagnostics() {
  const [conflicts, probes] = await Promise.all([api("/api/memory/conflicts"), api("/api/memory/probes")]);
  memoryConflicts.value = conflicts.items || [];
  memoryProbes.value = probes.items || [];
}

async function scanSkills() {
  await api("/api/skills/scan", { method: "POST" });
  await loadActive();
}

async function loadSkill(skillKey: unknown) {
  selectedSkill.value = await api(`/api/skills/${String(skillKey)}`);
}

async function testSkill(skillKey: unknown) {
  selectedSkill.value = await api(`/api/skills/${String(skillKey)}/test`, { method: "POST" });
}

async function rollbackSkill(skillKey: unknown) {
  await api(`/api/skills/${String(skillKey)}/rollback`, { method: "POST" });
  await loadActive();
}

async function runDream() {
  jobResult.value = await api("/api/dream/run", {
    method: "POST",
    body: JSON.stringify({ window_hours: 24, limit: 50 })
  });
  await loadActive();
  await loadDashboard();
}

async function cancelJob(id: unknown) {
  await api(`/api/jobs/${id}/cancel`, { method: "POST" });
  await loadActive();
}

async function applyProposal(id: unknown) {
  await api(`/api/evolution/proposals/${id}/apply`, { method: "POST", body: JSON.stringify({}) });
  await loadActive();
}

async function rejectProposal(id: unknown) {
  await api(`/api/evolution/proposals/${id}/reject`, { method: "POST", body: JSON.stringify({ reason: "Rejected in console" }) });
  await loadActive();
}

async function approveAndRun(id: unknown) {
  toolResult.value = await api(`/api/approvals/${id}/approve-and-run`, { method: "POST" });
  await loadActive();
}

async function rejectApproval(id: unknown) {
  await api(`/api/approvals/${id}/reject`, { method: "POST", body: JSON.stringify({ reason: "Rejected in console" }) });
  await loadActive();
}

async function runTool(name: unknown) {
  jsonError.value = "";
  const parsed = parseJson(toolArgs.value);
  if (parsed === null) return;
  try {
    toolResult.value = await api(`/api/tools/${String(name)}/run`, {
      method: "POST",
      body: JSON.stringify({ arguments: parsed })
    });
  } catch (err) {
    toolResult.value = { error: err instanceof Error ? err.message : String(err) };
  }
  await loadDashboard();
}

async function upsertCron() {
  jsonError.value = "";
  const metadata = parseJson(cronForm.value.metadata);
  if (metadata === null) return;
  await api("/api/cron", {
    method: "POST",
    body: JSON.stringify({ ...cronForm.value, metadata })
  });
  await loadActive();
}

async function upsertMcp() {
  jsonError.value = "";
  const config = parseJson(mcpForm.value.config);
  if (config === null) return;
  await api("/api/mcp", {
    method: "POST",
    body: JSON.stringify({ ...mcpForm.value, config })
  });
  await loadActive();
}

async function refreshMcp(name?: unknown) {
  await api(name ? `/api/mcp/${String(name)}/refresh` : "/api/mcp/refresh", { method: "POST" });
  await loadActive();
}

async function upsertGateway() {
  jsonError.value = "";
  const config = parseJson(gatewayForm.value.config);
  if (config === null) return;
  await api("/api/gateways", {
    method: "POST",
    body: JSON.stringify({ ...gatewayForm.value, config })
  });
  await loadActive();
}

async function testGatewayInbound() {
  jsonError.value = "";
  const metadata = parseJson(gatewayInbound.value.metadata);
  if (metadata === null) return;
  gatewayResult.value = await api("/api/gateways/inbound", {
    method: "POST",
    body: JSON.stringify({ ...gatewayInbound.value, metadata })
  });
  await loadActive();
}

async function testGatewaySend() {
  jsonError.value = "";
  const metadata = parseJson(gatewaySendForm.value.metadata);
  if (metadata === null) return;
  gatewayResult.value = await api("/api/gateways/send", {
    method: "POST",
    body: JSON.stringify({ ...gatewaySendForm.value, metadata })
  });
  await loadActive();
}

async function loadA2aTask(taskId: unknown) {
  selectedA2aTask.value = await api(`/api/a2a/tasks/${encodeURIComponent(String(taskId))}`);
}

async function syncActiveA2aTasks() {
  a2aResult.value = await api("/api/a2a/tasks/sync-active", {
    method: "POST",
    body: JSON.stringify({ enqueue: true, limit: 50 })
  });
  await loadActive();
}

async function syncA2aTask(taskId: unknown) {
  a2aResult.value = await api(`/api/a2a/tasks/${encodeURIComponent(String(taskId))}/sync`, { method: "POST" });
  await loadA2aTask(taskId);
  await loadActive();
}

async function cancelA2aTask(taskId: unknown) {
  a2aResult.value = await api(`/api/a2a/tasks/${encodeURIComponent(String(taskId))}/cancel`, { method: "POST" });
  await loadA2aTask(taskId);
  await loadActive();
}

async function resumeA2aRemoteApproval(taskId: unknown) {
  jsonError.value = "";
  let editedPayload: Record<string, unknown> = {};
  if (a2aResumeDecision.value === "edit") {
    const parsed = parseJson(a2aResumePayload.value);
    if (parsed === null) return;
    editedPayload = parsed;
  }
  a2aResult.value = await api(`/api/a2a/tasks/${encodeURIComponent(String(taskId))}/resume-remote-approval`, {
    method: "POST",
    body: JSON.stringify({
      decision: a2aResumeDecision.value,
      edited_payload: editedPayload,
      response: a2aResumeDecision.value === "respond" ? a2aResumePayload.value : ""
    })
  });
  await loadA2aTask(taskId);
  await loadActive();
}

async function loadSession(sessionId: unknown) {
  selectedSession.value = String(sessionId);
  sessionMessages.value = (await api(`/api/sessions/${encodeURIComponent(selectedSession.value)}/messages`)).items || [];
}

async function runTurn() {
  if (!turnMessage.value.trim()) return;
  lastTurn.value = await api("/api/runs/turn", {
    method: "POST",
    body: JSON.stringify({ message: turnMessage.value, session_id: "console" })
  });
  await loadDashboard();
  const turnContext = isRecord(lastTurn.value?.context) ? lastTurn.value.context : {};
  const turnRun = isRecord(turnContext.run) ? turnContext.run : {};
  const runId = String(turnRun.run_id || "");
  if (runId) {
    selectedRunId.value = runId;
    agentRuns.value = (await api("/api/agent-runs")).items || [];
  }
}

async function loadRunGraph(runId: unknown) {
  selectedRunId.value = String(runId || "");
  if (!selectedRunId.value) return;
  runGraph.value = await api(`/api/runs/${encodeURIComponent(selectedRunId.value)}/graph`);
}

async function rebuildVectors(sourceType = "") {
  vectorRebuildResult.value = await api("/api/vector/rebuild", {
    method: "POST",
    body: JSON.stringify({ source_type: sourceType, async_job: true })
  });
  vectorStatus.value = await api("/api/vector/status");
}

async function updatePolicyRule(rule: Record<string, unknown>, patch: Record<string, unknown>) {
  const ruleId = String(rule.rule_id || "");
  if (!ruleId) return;
  await api(`/api/policy/rules/${encodeURIComponent(ruleId)}`, {
    method: "PUT",
    body: JSON.stringify(patch)
  });
  const [status, rules] = await Promise.all([api("/api/policy/status"), api("/api/policy/rules")]);
  policyStatus.value = status;
  policyRules.value = rules.items || [];
}

function parseJson(value: string): Record<string, unknown> | null {
  try {
    const parsed = JSON.parse(value || "{}");
    if (!isRecord(parsed)) throw new Error("JSON must be an object");
    return parsed;
  } catch (err) {
    jsonError.value = err instanceof Error ? err.message : String(err);
    return null;
  }
}

function fmt(value: unknown) {
  if (value === null || value === undefined || value === "") return "-";
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  return String(value);
}

onMounted(refreshAll);
</script>

<template>
  <main v-if="!loggedIn" class="login-shell">
    <section class="login-panel">
      <div class="brand-row">
        <Database :size="28" />
        <div>
          <h1>SoulClaw</h1>
          <p>Platform Console</p>
        </div>
      </div>
      <form @submit.prevent="login" class="login-form">
        <label>
          <span>Username</span>
          <input v-model="username" autocomplete="username" />
        </label>
        <label>
          <span>Password</span>
          <input v-model="password" type="password" autocomplete="current-password" />
        </label>
        <button class="primary" type="submit" :disabled="loading">
          <KeyRound :size="18" />
          <span>{{ loading ? "Signing in" : "Sign in" }}</span>
        </button>
      </form>
      <p v-if="error" class="error">{{ error }}</p>
    </section>
  </main>

  <main v-else class="app-shell">
    <aside class="sidebar">
      <div class="brand-row compact">
        <Database :size="24" />
        <div>
          <h1>SoulClaw</h1>
          <p>{{ me?.username || "admin" }}</p>
        </div>
      </div>
      <nav>
        <button v-for="item in navItems" :key="item.key" :class="{ active: active === item.key }" @click="setView(item.key)">
          <component :is="item.icon" :size="18" />
          <span>{{ item.label }}</span>
        </button>
      </nav>
      <button class="ghost logout" @click="logout">
        <LogOut :size="18" />
        <span>Sign out</span>
      </button>
    </aside>

    <section class="content">
      <header class="topbar">
        <div>
          <h2>{{ pageTitle }}</h2>
          <p>{{ fmt(settingsData.environment) }}</p>
        </div>
        <button class="icon-button" @click="loadActive" title="Refresh">
          <RefreshCw :size="18" />
        </button>
      </header>

      <section v-if="active === 'dashboard'" class="stack">
        <div class="metrics">
          <article>
            <span>Wiki Pages</span>
            <strong>{{ wikiHealth.pages ?? 0 }}</strong>
          </article>
          <article>
            <span>Open Errors</span>
            <strong>{{ wikiHealth.open_errors ?? 0 }}</strong>
          </article>
          <article>
            <span>Storage</span>
            <strong>{{ settingsData.database_backend || "Postgres" }}</strong>
          </article>
          <article>
            <span>Dream Review</span>
            <strong>{{ dreamLabel }}</strong>
          </article>
          <article>
            <span>Heartbeat</span>
            <strong>{{ settingsData.heartbeat_enabled ? "On" : "Off" }}</strong>
          </article>
        </div>
        <section class="tool-surface">
          <textarea v-model="turnMessage" placeholder="Run a console turn"></textarea>
          <button class="primary" @click="runTurn">
            <Play :size="18" />
            <span>Run</span>
          </button>
        </section>
        <pre v-if="lastTurn">{{ fmt(lastTurn) }}</pre>
        <section class="list">
          <article v-for="event in events" :key="String(event.id)">
            <strong>{{ event.event_type }}</strong>
            <span>{{ event.severity }}</span>
            <p>{{ fmt(event.payload) }}</p>
          </article>
        </section>
      </section>

      <section v-if="active === 'wiki'" class="stack">
        <div class="actions">
          <button class="primary" @click="compileWiki">
            <RefreshCw :size="18" />
            <span>Compile</span>
          </button>
          <button class="secondary" @click="lintWiki">
            <ShieldCheck :size="18" />
            <span>Lint</span>
          </button>
          <div class="searchbox">
            <Search :size="18" />
            <input v-model="searchQuery" @keyup.enter="searchWiki" placeholder="Search wiki" />
          </div>
          <button class="secondary" @click="searchWiki">Search</button>
        </div>
        <pre v-if="wikiOrientation">{{ fmt({ orientation: wikiOrientation, lint: wikiLintResult, job: jobResult }) }}</pre>
        <section v-if="wikiErrors.length" class="alert-list">
          <article v-for="item in wikiErrors" :key="String(item.id)">
            <AlertTriangle :size="18" />
            <div>
              <strong>{{ item.error_type }} · {{ item.page_key }}</strong>
              <p>{{ item.root_cause }}</p>
            </div>
          </article>
        </section>
        <section class="list">
          <article v-for="item in displayedWikiItems" :key="item.page_key">
            <strong>{{ item.title }}</strong>
            <span>{{ item.page_key }}</span>
            <p>{{ item.summary }}</p>
            <button class="secondary fit" @click="readWiki(item.page_key)">Open</button>
          </article>
        </section>
        <pre v-if="selectedWikiPage">{{ fmt(selectedWikiPage) }}</pre>
      </section>

      <section v-if="active === 'memory'" class="stack">
        <section class="tool-surface">
          <select v-model="workspaceKind" @change="loadWorkspaceFile()">
            <option value="soul">SOUL.md</option>
            <option value="user">USER.md</option>
            <option value="memory">MEMORY.md</option>
            <option value="heartbeat">HEARTBEAT.md</option>
          </select>
          <button class="secondary" @click="loadWorkspaceFile()">Open File</button>
          <button class="primary" @click="importWorkspaceDraft">Import Projection Draft</button>
          <textarea v-model="workspaceContent" placeholder="Generated projection or draft content"></textarea>
        </section>
        <div class="actions">
          <div class="searchbox">
            <Search :size="18" />
            <input v-model="memoryQuery" @keyup.enter="searchMemory" placeholder="Search memory" />
          </div>
          <button class="secondary" @click="searchMemory">Search</button>
          <button class="secondary" @click="importMemoryDraft">Import Draft</button>
          <button class="secondary" @click="loadMemoryDiagnostics">Diagnostics</button>
        </div>
        <section class="tool-surface">
          <textarea v-model="newMemory" placeholder="Add memory"></textarea>
          <button class="primary" @click="createMemory">
            <Brain :size="18" />
            <span>Propose</span>
          </button>
        </section>
        <section class="list">
          <article v-for="item in memories" :key="String(item.id)">
            <strong>{{ item.kind }}</strong>
            <span>importance {{ item.importance }} · confidence {{ item.confidence }}</span>
            <p>{{ item.content }}</p>
            <div class="actions">
              <button class="secondary" @click="verifyMemory(item.id)">Verify</button>
              <button class="secondary" @click="archiveMemory(item.id)">Archive</button>
            </div>
          </article>
        </section>
        <pre v-if="memoryConflicts.length">{{ fmt({ conflicts: memoryConflicts, probes: memoryProbes }) }}</pre>
      </section>

      <section v-if="active === 'context'" class="stack">
        <section class="tool-surface">
          <select v-model="contextBlockKey">
            <option value="soul">SOUL</option>
            <option value="user">USER</option>
            <option value="heartbeat">HEARTBEAT</option>
          </select>
          <textarea v-model="contextDraft" placeholder="Draft a core context update proposal"></textarea>
          <button class="primary" @click="proposeContextBlock">Submit Proposal</button>
        </section>
        <section class="list">
          <article v-for="item in contextBlocks" :key="String(item.id)">
            <strong>{{ item.title || item.block_key }}</strong>
            <span>{{ item.block_key }} · v{{ item.version }} · confidence {{ item.confidence }}</span>
            <p>{{ item.content }}</p>
            <button class="secondary fit" @click="editContextBlock(item)">Edit Draft</button>
          </article>
        </section>
        <section class="list">
          <article v-for="item in contextProjections" :key="String(item.path)">
            <strong>{{ item.path }}</strong>
            <span>Generated projection</span>
            <pre>{{ item.content }}</pre>
          </article>
        </section>
      </section>

      <section v-if="active === 'skills'" class="stack">
        <button class="primary fit" @click="scanSkills">
          <RefreshCw :size="18" />
          <span>Scan</span>
        </button>
        <section class="list">
          <article v-for="item in skills" :key="String(item.id)">
            <strong>{{ item.name }}</strong>
            <span>{{ item.skill_key }} · {{ item.status }}</span>
            <p>{{ item.description }}</p>
            <div class="actions">
              <button class="secondary" @click="loadSkill(item.skill_key)">Open</button>
              <button class="secondary" @click="testSkill(item.skill_key)">Test</button>
              <button class="secondary" @click="rollbackSkill(item.skill_key)">Rollback</button>
            </div>
          </article>
        </section>
        <pre v-if="selectedSkill">{{ fmt(selectedSkill) }}</pre>
      </section>

      <section v-if="active === 'tools'" class="stack">
        <p v-if="jsonError" class="error">{{ jsonError }}</p>
        <section class="tool-surface">
          <textarea v-model="toolArgs" placeholder="Tool arguments JSON"></textarea>
          <button class="primary" @click="runTool(selectedTool)" :disabled="!selectedTool">
            <Play :size="18" />
            <span>Run</span>
          </button>
        </section>
        <section class="list">
          <article v-for="item in tools" :key="String(item.name)">
            <strong>{{ item.name }}</strong>
            <span>{{ item.scope }} · {{ item.available ? "available" : "unavailable" }} · {{ item.requires_approval ? "approval" : "direct" }}</span>
            <p>{{ item.description }}</p>
            <button class="secondary fit" @click="selectedTool = String(item.name)">Select</button>
          </article>
        </section>
        <pre v-if="toolResult">{{ fmt(toolResult) }}</pre>
      </section>

      <section v-if="active === 'cron'" class="stack">
        <p v-if="jsonError" class="error">{{ jsonError }}</p>
        <section class="form-grid">
          <input v-model="cronForm.name" placeholder="name" />
          <input v-model="cronForm.cron_expr" placeholder="cron" />
          <input v-model="cronForm.timezone" placeholder="timezone" />
          <label class="inline"><input v-model="cronForm.enabled" type="checkbox" /> enabled</label>
          <textarea v-model="cronForm.instruction" placeholder="instruction"></textarea>
          <textarea v-model="cronForm.metadata" placeholder="metadata JSON"></textarea>
          <button class="primary fit" @click="upsertCron">Save</button>
        </section>
        <section class="list">
          <article v-for="item in cronJobs" :key="String(item.id)">
            <strong>{{ item.name }}</strong>
            <span>{{ item.cron_expr }} · {{ item.timezone }} · {{ item.enabled ? "enabled" : "disabled" }}</span>
            <p>{{ item.instruction }}</p>
          </article>
        </section>
      </section>

      <section v-if="active === 'mcp'" class="stack">
        <p v-if="jsonError" class="error">{{ jsonError }}</p>
        <div class="actions"><button class="primary" @click="refreshMcp()">Refresh All</button></div>
        <section class="form-grid">
          <input v-model="mcpForm.name" placeholder="name" />
          <input v-model="mcpForm.transport" placeholder="transport" />
          <input v-model="mcpForm.command" placeholder="command" />
          <input v-model="mcpForm.url" placeholder="url" />
          <label class="inline"><input v-model="mcpForm.enabled" type="checkbox" /> enabled</label>
          <textarea v-model="mcpForm.config" placeholder="config JSON"></textarea>
          <button class="primary fit" @click="upsertMcp">Save</button>
        </section>
        <section class="list">
          <article v-for="item in mcpServers" :key="String(item.id)">
            <strong>{{ item.name }}</strong>
            <span>{{ item.transport }} · {{ item.enabled ? "enabled" : "disabled" }} · {{ item.status }} · {{ item.health_status || "unknown" }} · {{ item.session_mode || "transient" }} · {{ item.tool_count || 0 }} tools</span>
            <p>{{ item.command || item.url }}</p>
            <small>{{ item.config_source || "manual" }}</small>
            <small v-if="item.last_error">{{ item.last_error }}</small>
            <button class="secondary fit" @click="refreshMcp(item.name)">Refresh</button>
          </article>
        </section>
      </section>

      <section v-if="active === 'gateways'" class="stack">
        <p v-if="jsonError" class="error">{{ jsonError }}</p>
        <section class="form-grid">
          <input v-model="gatewayForm.name" placeholder="name" />
          <input v-model="gatewayForm.kind" placeholder="kind" />
          <input v-model="gatewayForm.endpoint" placeholder="endpoint" />
          <label class="inline"><input v-model="gatewayForm.enabled" type="checkbox" /> enabled</label>
          <textarea v-model="gatewayForm.config" placeholder="config JSON"></textarea>
          <button class="primary fit" @click="upsertGateway">Save</button>
        </section>
        <section class="form-grid">
          <input v-model="gatewayInbound.gateway_name" placeholder="inbound gateway" />
          <input v-model="gatewayInbound.external_user_id" placeholder="external user" />
          <input v-model="gatewayInbound.channel_id" placeholder="channel" />
          <textarea v-model="gatewayInbound.text" placeholder="inbound text"></textarea>
          <textarea v-model="gatewayInbound.metadata" placeholder="metadata JSON"></textarea>
          <button class="secondary fit" @click="testGatewayInbound">Test Inbound</button>
        </section>
        <section class="form-grid">
          <input v-model="gatewaySendForm.gateway_name" placeholder="send gateway" />
          <input v-model="gatewaySendForm.target_id" placeholder="target" />
          <textarea v-model="gatewaySendForm.text" placeholder="send text"></textarea>
          <textarea v-model="gatewaySendForm.metadata" placeholder="metadata JSON"></textarea>
          <button class="secondary fit" @click="testGatewaySend">Test Send</button>
        </section>
        <pre v-if="gatewayResult">{{ fmt(gatewayResult) }}</pre>
        <section class="list">
          <article v-for="item in gateways" :key="String(item.id)">
            <strong>{{ item.name }}</strong>
            <span>{{ item.kind }} · {{ item.status }} · in {{ item.inbound_count || 0 }} · out {{ item.outbound_count || 0 }}</span>
            <p>{{ item.endpoint }}</p>
            <small v-if="item.last_error">{{ item.last_error }}</small>
          </article>
        </section>
        <pre v-if="gatewayStatuses.length">{{ fmt({ statuses: gatewayStatuses }) }}</pre>
      </section>

      <section v-if="active === 'a2a'" class="stack">
        <div class="actions">
          <button class="primary" @click="syncActiveA2aTasks">
            <RefreshCw :size="18" />
            <span>Sync Active Remote Tasks</span>
          </button>
        </div>
        <section class="settings-grid">
          <article>
            <span>Connections</span>
            <strong>{{ a2aConnections.length }}</strong>
          </article>
          <article>
            <span>Tasks</span>
            <strong>{{ a2aTasks.length }}</strong>
          </article>
          <article>
            <span>Working</span>
            <strong>{{ a2aTasks.filter((item) => ['submitted', 'working', 'input-required', 'auth-required'].includes(String(item.status))).length }}</strong>
          </article>
          <article>
            <span>Approval Bridge</span>
            <strong>{{ a2aTasks.filter((item) => isRecord(item.metadata) && item.metadata.remote_approval_id).length }}</strong>
          </article>
        </section>
        <section class="list">
          <article v-for="item in a2aConnections" :key="String(item.id)">
            <strong>{{ item.name }}</strong>
            <span>{{ item.status }} · {{ item.enabled ? "enabled" : "disabled" }}</span>
            <p>{{ item.rpc_url || item.endpoint }}</p>
            <small v-if="item.last_error">{{ item.last_error }}</small>
          </article>
        </section>
        <section class="list">
          <article v-for="item in a2aTasks" :key="String(item.id)">
            <strong>{{ item.capability }} · {{ item.status }}</strong>
            <span>{{ item.connection_name }} · local {{ item.task_id }} · remote {{ item.remote_task_id || "-" }}</span>
            <p>{{ item.input_text }}</p>
            <div class="actions">
              <button class="secondary" @click="loadA2aTask(item.task_id)">Open</button>
              <button class="secondary" @click="syncA2aTask(item.task_id)" :disabled="!item.remote_task_id">Sync Remote</button>
              <button class="secondary" @click="cancelA2aTask(item.task_id)" :disabled="['completed', 'failed', 'canceled', 'rejected'].includes(String(item.status))">Cancel</button>
            </div>
            <pre>{{ fmt({ remote_context_id: item.remote_context_id, error: item.error, metadata: item.metadata }) }}</pre>
          </article>
        </section>
        <section v-if="selectedA2aTask" class="stack">
          <section class="tool-surface">
            <select v-model="a2aResumeDecision">
              <option value="approve">approve</option>
              <option value="edit">edit</option>
              <option value="reject">reject</option>
              <option value="respond">respond</option>
            </select>
            <textarea v-model="a2aResumePayload" placeholder="Edit JSON payload, or text response when decision=respond"></textarea>
            <button class="primary" @click="resumeA2aRemoteApproval(selectedA2aTask.task_id)" :disabled="!(isRecord(selectedA2aTask.metadata) && selectedA2aTask.metadata.remote_approval_id)">Resume Remote Approval</button>
          </section>
          <section class="settings-grid">
            <article>
              <span>Status</span>
              <strong>{{ selectedA2aTask.status }}</strong>
            </article>
            <article>
              <span>Remote</span>
              <strong>{{ selectedA2aTask.remote_task_id || "-" }}</strong>
            </article>
            <article>
              <span>Artifacts</span>
              <strong>{{ Array.isArray(selectedA2aTask.artifacts) ? selectedA2aTask.artifacts.length : 0 }}</strong>
            </article>
            <article>
              <span>Events</span>
              <strong>{{ Array.isArray(selectedA2aTask.events) ? selectedA2aTask.events.length : 0 }}</strong>
            </article>
          </section>
          <pre>{{ fmt({ result: selectedA2aTask.result, error: selectedA2aTask.error, metadata: selectedA2aTask.metadata }) }}</pre>
          <section class="list" v-if="Array.isArray(selectedA2aTask.artifacts)">
            <article v-for="artifact in selectedA2aTask.artifacts" :key="String(artifact.id)">
              <strong>{{ artifact.name || artifact.artifact_id }}</strong>
              <span>{{ artifact.mime_type }} · {{ artifact.created_at }}</span>
              <pre>{{ fmt({ content: artifact.content, parts: artifact.parts, metadata: artifact.metadata }) }}</pre>
            </article>
          </section>
          <section class="list" v-if="Array.isArray(selectedA2aTask.events)">
            <article v-for="event in selectedA2aTask.events" :key="String(event.id)">
              <strong>{{ event.sequence }} · {{ event.event_type }}</strong>
              <span>{{ event.created_at }}</span>
              <pre>{{ fmt(event.payload) }}</pre>
            </article>
          </section>
        </section>
        <pre v-if="a2aResult">{{ fmt(a2aResult) }}</pre>
      </section>

      <section v-if="active === 'heartbeat'" class="stack">
        <div class="actions">
          <button class="primary" @click="runHeartbeat(true)">Enqueue Heartbeat</button>
          <button class="secondary" @click="runHeartbeat(false)">Run Now</button>
          <button class="secondary" @click="loadWorkspaceFile('heartbeat')">Open Projection</button>
        </div>
        <section class="tool-surface">
          <textarea v-model="workspaceContent" placeholder="Heartbeat projection or draft"></textarea>
          <button class="primary" @click="workspaceKind = 'heartbeat'; importWorkspaceDraft()">Import Heartbeat Draft</button>
        </section>
        <pre>{{ fmt({ status: heartbeatStatus, files: workspaceFiles, job: jobResult }) }}</pre>
      </section>

      <section v-if="active === 'reliability'" class="stack">
        <div class="metrics">
          <article>
            <span>Alerts</span>
            <strong>{{ reliabilityAlerts.length }}</strong>
          </article>
          <article>
            <span>Dead Letter</span>
            <strong>{{ isRecord(reliabilityStatus.jobs) ? reliabilityStatus.jobs.dead_letter || 0 : 0 }}</strong>
          </article>
          <article>
            <span>Outbox Pending</span>
            <strong>{{ isRecord(reliabilityStatus.outbox) ? Number(reliabilityStatus.outbox.pending || 0) + Number(reliabilityStatus.outbox.retrying || 0) : 0 }}</strong>
          </article>
          <article>
            <span>Cron Backoff</span>
            <strong>{{ isRecord(reliabilityStatus.cron) ? reliabilityStatus.cron.backoff || 0 : 0 }}</strong>
          </article>
        </div>
        <section v-if="reliabilityAlerts.length" class="alert-list">
          <article v-for="item in reliabilityAlerts" :key="String(item.kind) + String(item.message)">
            <AlertTriangle :size="18" />
            <div>
              <strong>{{ item.severity }} · {{ item.kind }}</strong>
              <p>{{ item.message }}</p>
            </div>
          </article>
        </section>
        <section class="settings-grid">
          <article>
            <span>Jobs</span>
            <strong>{{ fmt(reliabilityStatus.jobs) }}</strong>
          </article>
          <article>
            <span>Outbox</span>
            <strong>{{ fmt(reliabilityStatus.outbox) }}</strong>
          </article>
          <article>
            <span>Rate Limit</span>
            <strong>{{ fmt(reliabilityLimits.policies || reliabilityStatus.rate_limit) }}</strong>
          </article>
          <article>
            <span>Resilience</span>
            <strong>{{ fmt(reliabilityStatus.resilience) }}</strong>
          </article>
        </section>
        <section class="list">
          <article v-for="item in recentTraces" :key="String(item.id)">
            <strong>{{ item.event_type }}</strong>
            <span>{{ item.trace_id }} · {{ item.severity }}</span>
            <p>{{ fmt(item.payload) }}</p>
          </article>
        </section>
      </section>

      <section v-if="active === 'sessions'" class="stack">
        <section class="list">
          <article v-for="item in sessions" :key="String(item.session_id)">
            <strong>{{ item.session_id }}</strong>
            <span>{{ item.message_count }} messages · {{ item.last_message_at }}</span>
            <button class="secondary fit" @click="loadSession(item.session_id)">Open</button>
          </article>
        </section>
        <section v-if="sessionMessages.length" class="list">
          <article v-for="item in sessionMessages" :key="String(item.id)">
            <strong>{{ item.role }}</strong>
            <span>{{ item.turn_id }} · {{ item.created_at }}</span>
            <p>{{ item.content }}</p>
          </article>
        </section>
      </section>

      <section v-if="active === 'jobs'" class="stack">
        <section class="list">
          <article v-for="item in jobs" :key="String(item.id)">
            <strong>{{ item.task_name }}</strong>
            <span>{{ item.status }} · attempt {{ item.attempt_count || 0 }}/{{ item.max_attempts || 0 }} · {{ item.created_at }}</span>
            <div class="actions">
              <button class="secondary" @click="cancelJob(item.id)" :disabled="!['queued', 'running', 'retrying'].includes(String(item.status))">Cancel</button>
            </div>
            <pre>{{ fmt({ payload: item.payload, result: item.result, error: item.error, queue_id: item.queue_id, next_retry_at: item.next_retry_at, dead_letter_reason: item.dead_letter_reason, trace_id: item.trace_id }) }}</pre>
          </article>
        </section>
      </section>

      <section v-if="active === 'proposals'" class="stack">
        <div class="actions">
          <button class="primary" @click="runDream">
            <RefreshCw :size="18" />
            <span>Run Dream</span>
          </button>
        </div>
        <pre v-if="jobResult">{{ fmt(jobResult) }}</pre>
        <div class="list">
          <article v-for="item in proposals" :key="String(item.id)">
            <strong>{{ item.action }} · {{ item.target_type }}</strong>
            <span>{{ item.status }} · {{ item.risk_level }}</span>
            <div class="actions">
              <button class="secondary" @click="applyProposal(item.id)">Apply</button>
              <button class="secondary" @click="rejectProposal(item.id)">Reject</button>
            </div>
            <pre>{{ fmt(item.result || item.payload) }}</pre>
          </article>
        </div>
      </section>

      <section v-if="active === 'approvals'" class="list">
        <article v-for="item in approvals" :key="String(item.id)">
          <strong>{{ item.subject_type }}</strong>
          <span>{{ item.status }}</span>
          <div class="actions">
            <button class="secondary" @click="approveAndRun(item.id)">Approve & Run</button>
            <button class="secondary" @click="rejectApproval(item.id)">Reject</button>
          </div>
          <pre>{{ fmt(item.payload) }}</pre>
        </article>
        <pre v-if="toolResult">{{ fmt(toolResult) }}</pre>
      </section>

      <section v-if="active === 'runs'" class="list">
        <article v-for="item in runs" :key="String(item.id)">
          <strong>{{ item.tool_name }}</strong>
          <span>{{ item.status }} · {{ item.turn_id }}</span>
          <pre>{{ fmt(item.result) }}</pre>
        </article>
      </section>

      <section v-if="active === 'runGraph'" class="stack">
        <div class="actions">
          <select v-model="selectedRunId" @change="loadRunGraph(selectedRunId)">
            <option v-for="item in agentRuns" :key="String(item.run_id)" :value="String(item.run_id)">
              {{ item.status }} · {{ item.turn_id }} · {{ item.started_at }}
            </option>
          </select>
          <button class="secondary" @click="loadRunGraph(selectedRunId)" :disabled="!selectedRunId">Load Graph</button>
        </div>
        <section v-if="runGraph && isRecord(runGraph.run)" class="settings-grid">
          <article>
            <span>Run</span>
            <strong>{{ runGraph.run.run_id }}</strong>
          </article>
          <article>
            <span>Status</span>
            <strong>{{ runGraph.run.status }}</strong>
          </article>
          <article>
            <span>Engine</span>
            <strong>{{ runGraph.run.engine }}</strong>
          </article>
          <article>
            <span>Thread</span>
            <strong>{{ runGraph.run.thread_id }}</strong>
          </article>
        </section>
        <section v-if="runGraph && Array.isArray(runGraph.nodes)" class="graph-list">
          <article v-for="node in runGraph.nodes" :key="String(node.sequence) + String(node.id)" :class="String(node.status)">
            <strong>{{ node.id }}</strong>
            <span>{{ node.status }} · {{ node.finished_at || node.started_at }}</span>
            <pre>{{ fmt({ input: node.input, output: node.output, error: node.error }) }}</pre>
          </article>
        </section>
      </section>

      <section v-if="active === 'vector'" class="stack">
        <div class="actions">
          <button class="primary" @click="rebuildVectors('')">
            <RefreshCw :size="18" />
            <span>Rebuild All</span>
          </button>
          <button class="secondary" @click="rebuildVectors('wiki')">Wiki</button>
          <button class="secondary" @click="rebuildVectors('memory')">Memory</button>
          <button class="secondary" @click="rebuildVectors('skill')">Skills</button>
        </div>
        <section class="settings-grid">
          <article>
            <span>Mode</span>
            <strong>{{ vectorStatus.mode }}</strong>
          </article>
          <article>
            <span>Backend</span>
            <strong>{{ vectorStatus.backend }}</strong>
          </article>
          <article>
            <span>Model</span>
            <strong>{{ vectorStatus.embedding_model }}</strong>
          </article>
          <article>
            <span>Dimensions</span>
            <strong>{{ vectorStatus.embedding_dimensions }}</strong>
          </article>
        </section>
        <pre>{{ fmt({ status: vectorStatus, rebuild: vectorRebuildResult }) }}</pre>
      </section>

      <section v-if="active === 'policy'" class="stack">
        <section class="settings-grid">
          <article>
            <span>Rules</span>
            <strong>{{ policyStatus.rules }}</strong>
          </article>
          <article>
            <span>Approval</span>
            <strong>{{ policyStatus.approval_required }}</strong>
          </article>
          <article>
            <span>Denied</span>
            <strong>{{ policyStatus.denied }}</strong>
          </article>
          <article>
            <span>Engine</span>
            <strong>{{ policyStatus.enabled ? "Enabled" : "Off" }}</strong>
          </article>
        </section>
        <section class="list">
          <article v-for="rule in policyRules" :key="String(rule.rule_id)">
            <strong>{{ rule.subject }}</strong>
            <span>{{ rule.scope }} · {{ rule.risk_level }} · {{ rule.action }} · {{ rule.requires_approval ? "approval" : "direct" }}</span>
            <div class="actions">
              <button class="secondary" @click="updatePolicyRule(rule, { enabled: !rule.enabled })">
                {{ rule.enabled ? "Disable" : "Enable" }}
              </button>
              <button class="secondary" @click="updatePolicyRule(rule, { requires_approval: !rule.requires_approval })">
                {{ rule.requires_approval ? "Direct" : "Approval" }}
              </button>
              <button class="secondary" @click="updatePolicyRule(rule, { action: rule.action === 'deny' ? 'allow' : 'deny' })">
                {{ rule.action === "deny" ? "Allow" : "Deny" }}
              </button>
            </div>
            <pre>{{ fmt(rule.config) }}</pre>
          </article>
        </section>
      </section>

      <section v-if="active === 'audit'" class="stack">
        <section class="list">
          <article v-for="item in audit" :key="String(item.id)">
            <strong>{{ item.action }}</strong>
            <span>{{ item.target_type }} · {{ item.created_at }}</span>
            <pre>{{ fmt(item.payload) }}</pre>
          </article>
        </section>
      </section>

      <section v-if="active === 'settings'" class="settings-grid">
        <article v-for="(value, key) in settingsData" :key="key">
          <span>{{ key }}</span>
          <strong>{{ fmt(value) }}</strong>
        </article>
      </section>
    </section>
  </main>
</template>
